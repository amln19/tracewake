package artifacts

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// Filesystem stores objects locally and hands out URLs signed with the same
// short lifetime as hosted object storage, so both deployments exercise one
// worker and tenant protocol.
type Filesystem struct {
	root    string
	signing []byte
	now     func() time.Time
}

func NewFilesystem(root string, signingKey []byte) (*Filesystem, error) {
	if root == "" {
		return nil, errors.New("artifact root is required")
	}
	if len(signingKey) < 32 {
		return nil, errors.New("object URL signing key must contain at least 32 bytes")
	}
	if err := os.MkdirAll(root, 0o700); err != nil {
		return nil, fmt.Errorf("create artifact root: %w", err)
	}
	return &Filesystem{root: root, signing: signingKey, now: time.Now}, nil
}

func (s *Filesystem) PutGrant(_ context.Context, key, digest string, size int64, mediaType string) (Grant, error) {
	if !validDeclaration(key, digest, size) {
		return Grant{}, errors.New("artifact declaration is invalid")
	}
	expires := s.now().UTC().Add(GrantLifetime)
	return Grant{
		URL:       s.signedURL("PUT", key, "", digest, size, expires),
		Method:    "PUT",
		Headers:   map[string]string{"Content-Type": mediaType},
		ExpiresAt: expires,
	}, nil
}

func (s *Filesystem) GetGrant(_ context.Context, key, version, _ string) (Grant, error) {
	if !safeKey(key) || len(version) != 64 {
		return Grant{}, errors.New("artifact identity is invalid")
	}
	expires := s.now().UTC().Add(GrantLifetime)
	return Grant{
		URL:       s.signedURL("GET", key, version, "", 0, expires),
		Method:    "GET",
		Headers:   map[string]string{},
		ExpiresAt: expires,
	}, nil
}

// Commit re-reads the stored bytes because a local store has no server-side
// checksum to trust.
func (s *Filesystem) Commit(_ context.Context, key, version, digest string, size int64) (Object, error) {
	if !validDeclaration(key, digest, size) || version != digest {
		return Object{}, errors.New("artifact identity is invalid")
	}
	file, err := s.open(key)
	if err != nil {
		return Object{}, err
	}
	defer file.Close()
	hash := sha256.New()
	actualSize, err := io.Copy(hash, file)
	if err != nil {
		return Object{}, fmt.Errorf("hash artifact: %w", err)
	}
	actual := hex.EncodeToString(hash.Sum(nil))
	if actualSize != size || actual != digest {
		return Object{}, errors.New("artifact identity does not match stored bytes")
	}
	return Object{Key: key, Version: actual, Digest: actual, Size: actualSize}, nil
}

func (s *Filesystem) Open(_ context.Context, key, version string) (io.ReadCloser, error) {
	if len(version) != 64 {
		return nil, errors.New("artifact identity is invalid")
	}
	return s.open(key)
}

func (s *Filesystem) Put(_ context.Context, key, expectedDigest string, expectedSize int64, _ string, input io.Reader) (Object, error) {
	if !validDeclaration(key, expectedDigest, expectedSize) {
		return Object{}, errors.New("artifact declaration is invalid")
	}
	target := filepath.Join(s.root, filepath.FromSlash(key))
	if err := os.MkdirAll(filepath.Dir(target), 0o700); err != nil {
		return Object{}, fmt.Errorf("create artifact parent: %w", err)
	}
	temporary, err := os.CreateTemp(filepath.Dir(target), ".upload-")
	if err != nil {
		return Object{}, fmt.Errorf("create artifact staging file: %w", err)
	}
	temporaryName := temporary.Name()
	defer func() { _ = os.Remove(temporaryName) }()
	hash := sha256.New()
	count, err := io.Copy(io.MultiWriter(temporary, hash), io.LimitReader(input, expectedSize+1))
	if closeErr := temporary.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return Object{}, fmt.Errorf("write artifact: %w", err)
	}
	if count != expectedSize {
		return Object{}, fmt.Errorf("artifact size is %d, expected %d", count, expectedSize)
	}
	digest := hex.EncodeToString(hash.Sum(nil))
	if digest != expectedDigest {
		return Object{}, errors.New("artifact digest does not match declaration")
	}
	if err := os.Rename(temporaryName, target); err != nil {
		return Object{}, fmt.Errorf("publish artifact: %w", err)
	}
	return Object{Key: key, Version: digest, Digest: digest, Size: count}, nil
}

func (s *Filesystem) put(key, expectedDigest string, expectedSize int64, input io.Reader) (Object, error) {
	return s.Put(context.Background(), key, expectedDigest, expectedSize, "", input)
}

func (s *Filesystem) open(key string) (*os.File, error) {
	if !safeKey(key) {
		return nil, errors.New("artifact identity is invalid")
	}
	file, err := os.Open(filepath.Join(s.root, filepath.FromSlash(key)))
	if err != nil {
		return nil, fmt.Errorf("open artifact: %w", err)
	}
	return file, nil
}

func (s *Filesystem) Cleanup(_ context.Context, keep map[string]bool, before time.Time) (int, error) {
	removed := 0
	err := filepath.WalkDir(s.root, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			return nil
		}
		relative, err := filepath.Rel(s.root, path)
		if err != nil {
			return err
		}
		key := filepath.ToSlash(relative)
		if keep[key] {
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if info.ModTime().After(before) {
			return nil
		}
		if err := os.Remove(path); err != nil {
			return err
		}
		removed++
		return nil
	})
	if err != nil {
		return removed, fmt.Errorf("clean orphan artifacts: %w", err)
	}
	return removed, nil
}

func (s *Filesystem) signedURL(method, key, version, digest string, size int64, expires time.Time) string {
	query := url.Values{}
	query.Set("expires", strconv.FormatInt(expires.Unix(), 10))
	if version != "" {
		query.Set("version", version)
	}
	if digest != "" {
		query.Set("digest", digest)
		query.Set("size", strconv.FormatInt(size, 10))
	}
	query.Set("signature", s.sign(method, key, version, digest, size, expires.Unix()))
	return "/objects/" + escapeKey(key) + "?" + query.Encode()
}

func (s *Filesystem) sign(method, key, version, digest string, size int64, expires int64) string {
	mac := hmac.New(sha256.New, s.signing)
	for _, field := range []string{method, key, version, digest, strconv.FormatInt(size, 10), strconv.FormatInt(expires, 10)} {
		_, _ = mac.Write([]byte(field))
		_, _ = mac.Write([]byte{0})
	}
	return hex.EncodeToString(mac.Sum(nil))
}

func escapeKey(key string) string {
	segments := strings.Split(key, "/")
	for index, segment := range segments {
		segments[index] = url.PathEscape(segment)
	}
	return strings.Join(segments, "/")
}
