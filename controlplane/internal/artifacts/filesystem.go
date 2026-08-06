package artifacts

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

const MaxBundleSize int64 = 256 * 1024 * 1024

type Store struct{ root string }

type Object struct {
	Key     string
	Version string
	Digest  string
	Size    int64
}

func New(root string) (*Store, error) {
	if root == "" {
		return nil, errors.New("artifact root is required")
	}
	if err := os.MkdirAll(root, 0o700); err != nil {
		return nil, fmt.Errorf("create artifact root: %w", err)
	}
	return &Store{root: root}, nil
}

func (s *Store) Put(key, expectedDigest string, expectedSize int64, input io.Reader) (Object, error) {
	if !safeKey(key) {
		return Object{}, errors.New("artifact key is invalid")
	}
	if expectedSize < 0 || expectedSize > MaxBundleSize || len(expectedDigest) != 64 {
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

func (s *Store) Open(key, version string) (*os.File, error) {
	if !safeKey(key) || len(version) != 64 {
		return nil, errors.New("artifact identity is invalid")
	}
	file, err := os.Open(filepath.Join(s.root, filepath.FromSlash(key)))
	if err != nil {
		return nil, fmt.Errorf("open artifact: %w", err)
	}
	return file, nil
}

func safeKey(key string) bool {
	return key != "" && !strings.HasPrefix(key, "/") && filepath.ToSlash(filepath.Clean(key)) == key && !strings.Contains(key, "..")
}
