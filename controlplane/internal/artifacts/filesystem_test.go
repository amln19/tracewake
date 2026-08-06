package artifacts

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"testing"
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
