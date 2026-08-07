package artifacts

import (
	"context"
	"io"
	"path/filepath"
	"strings"
	"time"
)

const MaxBundleSize int64 = 256 * 1024 * 1024

// MaxResultSize bounds one attempt output. Analyses summarise a bundle rather
// than copying it, so a larger output means a defect, not a large run.
const MaxResultSize int64 = 64 * 1024 * 1024

// GrantLifetime bounds how long a caller may use a short-lived object URL.
const GrantLifetime = 15 * time.Minute

type Object struct {
	Key     string `json:"object_key"`
	Version string `json:"object_version"`
	Digest  string `json:"digest"`
	Size    int64  `json:"size"`
}

// Grant is short-lived access to one server-generated object key. A relative
// URL is served by this control plane; an absolute URL belongs to the object
// store itself.
type Grant struct {
	URL       string            `json:"url"`
	Method    string            `json:"method"`
	Headers   map[string]string `json:"headers"`
	ExpiresAt time.Time         `json:"expires_at"`
}

type Store interface {
	PutGrant(ctx context.Context, key, digest string, size int64, mediaType string) (Grant, error)
	GetGrant(ctx context.Context, key, version, mediaType string) (Grant, error)
	Commit(ctx context.Context, key, version, digest string, size int64) (Object, error)
	Open(ctx context.Context, key, version string) (io.ReadCloser, error)
	Cleanup(ctx context.Context, keep map[string]bool, before time.Time) (int, error)
}

// Absolute resolves a control-plane-relative grant against the base URL the
// caller can reach. Object-store URLs are already absolute.
func Absolute(base, target string) string {
	if strings.HasPrefix(target, "http://") || strings.HasPrefix(target, "https://") {
		return target
	}
	return strings.TrimSuffix(base, "/") + target
}

func safeKey(key string) bool {
	return key != "" && len(key) <= 512 && !strings.HasPrefix(key, "/") &&
		filepath.ToSlash(filepath.Clean(key)) == key && !strings.Contains(key, "..")
}

func validDeclaration(key, digest string, size int64) bool {
	return safeKey(key) && len(digest) == 64 && size >= 0 && size <= MaxBundleSize
}
