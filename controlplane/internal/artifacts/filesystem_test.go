package artifacts

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func testStore(t *testing.T) (*Filesystem, string) {
	t.Helper()
	root := t.TempDir()
	store, err := NewFilesystem(root, []byte("object-url-signing-key-for-tests!"))
	if err != nil {
		t.Fatal(err)
	}
	return store, root
}

func sha256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func upload(t *testing.T, store *Filesystem, key string, data []byte) Object {
	t.Helper()
	grant, err := store.PutGrant(context.Background(), key, sha256Hex(data), int64(len(data)), "application/json")
	if err != nil {
		t.Fatal(err)
	}
	response := httptest.NewRecorder()
	store.Handler().ServeHTTP(response, httptest.NewRequest("PUT", grant.URL, bytes.NewReader(data)))
	if response.Code != http.StatusCreated {
		t.Fatalf("upload status=%d body=%s", response.Code, response.Body)
	}
	object, err := store.Commit(context.Background(), key, response.Header().Get("Tracewake-Object-Version"), sha256Hex(data), int64(len(data)))
	if err != nil {
		t.Fatal(err)
	}
	return object
}

func TestSignedGrantStoresAndServesObject(t *testing.T) {
	store, _ := testStore(t)
	data := []byte("verified artifact")
	object := upload(t, store, "workspaces/a/runs/b/bundle.tar", data)
	if object.Version != object.Digest {
		t.Fatal("local object version must be its immutable content digest")
	}
	grant, err := store.GetGrant(context.Background(), object.Key, object.Version, "application/json")
	if err != nil {
		t.Fatal(err)
	}
	response := httptest.NewRecorder()
	store.Handler().ServeHTTP(response, httptest.NewRequest("GET", grant.URL, nil))
	if response.Code != http.StatusOK {
		t.Fatalf("download status=%d", response.Code)
	}
	if got, _ := io.ReadAll(response.Body); !bytes.Equal(got, data) {
		t.Fatalf("got %q", got)
	}
	if response.Header().Get("Content-Disposition") != "attachment" || response.Header().Get("X-Content-Type-Options") != "nosniff" {
		t.Fatalf("sensitive artifacts must not render inline: %v", response.Header())
	}
}

func TestControlPlanePutValidatesAndPublishesBytes(t *testing.T) {
	store, root := testStore(t)
	data := []byte("browser bundle")
	key := "workspaces/a/runs/browser/bundle.tar"
	object, err := store.Put(context.Background(), key, sha256Hex(data), int64(len(data)), "application/x-tar", bytes.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	if object.Version != sha256Hex(data) {
		t.Fatalf("object version=%q", object.Version)
	}
	if got, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(key))); err != nil || !bytes.Equal(got, data) {
		t.Fatalf("stored bytes=%q err=%v", got, err)
	}
	if _, err := store.Put(context.Background(), key, sha256Hex(data), int64(len(data)), "application/x-tar", strings.NewReader("wrong bytes!!!")); err == nil {
		t.Fatal("mismatched browser upload was accepted")
	}
}

func TestUploadGrantRejectsMismatchedBytes(t *testing.T) {
	store, root := testStore(t)
	key := "workspaces/a/runs/b/bundle.tar"
	grant, err := store.PutGrant(context.Background(), key, sha256Hex([]byte("declared")), 8, "application/json")
	if err != nil {
		t.Fatal(err)
	}
	response := httptest.NewRecorder()
	store.Handler().ServeHTTP(response, httptest.NewRequest("PUT", grant.URL, strings.NewReader("replaced")))
	if response.Code != http.StatusBadRequest {
		t.Fatalf("status=%d", response.Code)
	}
	if _, err := os.Stat(filepath.Join(root, filepath.FromSlash(key))); !os.IsNotExist(err) {
		t.Fatalf("rejected upload was published: %v", err)
	}
}

func TestGrantRejectsTamperedExpiredAndForeignRequests(t *testing.T) {
	store, _ := testStore(t)
	data := []byte("private artifact")
	object := upload(t, store, "workspaces/a/jobs/b/attempts/1/diff_json", data)
	grant, err := store.GetGrant(context.Background(), object.Key, object.Version, "application/json")
	if err != nil {
		t.Fatal(err)
	}
	cases := map[string]string{
		"tampered key":       strings.Replace(grant.URL, "workspaces/a", "workspaces/c", 1),
		"tampered signature": strings.Replace(grant.URL, "signature=", "signature=00", 1),
		"missing signature":  strings.Split(grant.URL, "&signature=")[0],
		"traversal":          "/objects/../../etc/passwd?" + strings.SplitN(grant.URL, "?", 2)[1],
	}
	for name, target := range cases {
		response := httptest.NewRecorder()
		store.Handler().ServeHTTP(response, httptest.NewRequest("GET", target, nil))
		if response.Code == http.StatusOK {
			t.Fatalf("%s was served", name)
		}
	}
	expired := *store
	expired.now = func() time.Time { return time.Now().Add(GrantLifetime + time.Minute) }
	response := httptest.NewRecorder()
	expired.Handler().ServeHTTP(response, httptest.NewRequest("GET", grant.URL, nil))
	if response.Code != http.StatusForbidden {
		t.Fatalf("expired grant status=%d", response.Code)
	}
	response = httptest.NewRecorder()
	store.Handler().ServeHTTP(response, httptest.NewRequest("PUT", grant.URL, strings.NewReader("evil")))
	if response.Code == http.StatusCreated {
		t.Fatal("download grant was reused to write")
	}
}

func TestCommitDetectsPostUploadCorruption(t *testing.T) {
	store, root := testStore(t)
	data := []byte("good")
	object := upload(t, store, "workspaces/a/jobs/b/attempts/1/diff_json", data)
	if err := os.WriteFile(filepath.Join(root, filepath.FromSlash(object.Key)), []byte("evil"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Commit(context.Background(), object.Key, object.Version, object.Digest, object.Size); err == nil {
		t.Fatal("corrupted artifact committed")
	}
}

func TestCleanupRemovesOnlyExpiredOrphans(t *testing.T) {
	store, root := testStore(t)
	for _, key := range []string{"kept/result", "orphan/old", "orphan/new"} {
		upload(t, store, key, []byte("data"))
	}
	old := time.Now().Add(-25 * time.Hour)
	if err := os.Chtimes(filepath.Join(root, "orphan/old"), old, old); err != nil {
		t.Fatal(err)
	}
	removed, err := store.Cleanup(context.Background(), map[Identity]bool{{Key: "kept/result", Version: sha256Hex([]byte("data"))}: true}, time.Now().Add(-24*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if removed != 1 {
		t.Fatalf("removed=%d", removed)
	}
	for _, key := range []string{"kept/result", "orphan/new"} {
		if _, err := os.Stat(filepath.Join(root, filepath.FromSlash(key))); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := os.Stat(filepath.Join(root, "orphan/old")); !os.IsNotExist(err) {
		t.Fatalf("old orphan remains: %v", err)
	}
}
