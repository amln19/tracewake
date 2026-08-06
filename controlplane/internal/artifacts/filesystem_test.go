package artifacts

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestPutVerifiesAndPublishesArtifact(t *testing.T) {
	store, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	data := []byte("verified artifact")
	digest := sha256.Sum256(data)
	object, err := store.Put("workspaces/a/uploads/bundle.tar", hex.EncodeToString(digest[:]), int64(len(data)), bytes.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	if object.Version != object.Digest {
		t.Fatal("filesystem version must be immutable content digest")
	}
	file, err := store.Open(object.Key, object.Version)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	got, err := io.ReadAll(file)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, data) {
		t.Fatalf("got %q", got)
	}
}

func TestPutRejectsInvalidIdentity(t *testing.T) {
	store, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Put("../escape", "not-a-digest", 0, bytes.NewReader(nil)); err == nil {
		t.Fatal("unsafe key accepted")
	}
}

func TestPutRejectsDigestMismatchWithoutPublishing(t *testing.T) {
	root := t.TempDir()
	store, err := New(root)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Put("workspaces/a/uploads/bundle.tar", strings.Repeat("0", 64), 3, bytes.NewReader([]byte("bad"))); err == nil {
		t.Fatal("digest mismatch accepted")
	}
	if _, err := os.Stat(filepath.Join(root, "workspaces/a/uploads/bundle.tar")); !os.IsNotExist(err) {
		t.Fatalf("failed upload was published: %v", err)
	}
}

func TestVerifyDetectsPostUploadCorruption(t *testing.T) {
	root := t.TempDir()
	store, err := New(root)
	if err != nil {
		t.Fatal(err)
	}
	data := []byte("good")
	sum := sha256.Sum256(data)
	digest := hex.EncodeToString(sum[:])
	object, err := store.Put("workspaces/a/jobs/b/attempts/1/result", digest, int64(len(data)), bytes.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, filepath.FromSlash(object.Key)), []byte("evil"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := store.Verify(object.Key, object.Version, object.Digest, object.Size); err == nil {
		t.Fatal("corrupted artifact verified")
	}
}

func TestCleanupRemovesOnlyExpiredOrphans(t *testing.T) {
	root := t.TempDir()
	store, err := New(root)
	if err != nil {
		t.Fatal(err)
	}
	data := []byte("data")
	sum := sha256.Sum256(data)
	digest := hex.EncodeToString(sum[:])
	for _, key := range []string{"kept/result", "orphan/old", "orphan/new"} {
		if _, err := store.Put(key, digest, int64(len(data)), bytes.NewReader(data)); err != nil {
			t.Fatal(err)
		}
	}
	old := time.Now().Add(-25 * time.Hour)
	if err := os.Chtimes(filepath.Join(root, "orphan/old"), old, old); err != nil {
		t.Fatal(err)
	}
	removed, err := store.Cleanup(map[string]bool{"kept/result": true}, time.Now().Add(-24*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if removed != 1 {
		t.Fatalf("removed=%d", removed)
	}
	if _, err := os.Stat(filepath.Join(root, "kept/result")); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, "orphan/new")); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, "orphan/old")); !os.IsNotExist(err) {
		t.Fatalf("old orphan remains: %v", err)
	}
}
