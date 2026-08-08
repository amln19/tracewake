package artifacts_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/amln19/tracewake/controlplane/internal/artifacts"
	"github.com/amln19/tracewake/controlplane/internal/awstest"
	"github.com/aws/aws-sdk-go-v2/service/s3"
)

func s3Store(t *testing.T) (*artifacts.S3, *awstest.S3) {
	t.Helper()
	fake := awstest.NewS3("tracewake-artifacts")
	t.Cleanup(fake.Close)
	store, err := artifacts.NewS3(awstest.Config(fake.Server.URL), fake.Bucket, func(options *s3.Options) {
		options.UsePathStyle = true
	})
	if err != nil {
		t.Fatal(err)
	}
	return store, fake
}

func hexDigest(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func send(t *testing.T, grant artifacts.Grant, body []byte) *http.Response {
	t.Helper()
	request, err := http.NewRequest(grant.Method, grant.URL, bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	for name, value := range grant.Headers {
		request.Header.Set(name, value)
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	return response
}

func TestPresignedUploadCommitsExactObjectVersion(t *testing.T) {
	store, _ := s3Store(t)
	ctx := context.Background()
	data := []byte(`{"result":"canonical"}`)
	key := "workspaces/w/jobs/j/attempts/1/diff_json"
	grant, err := store.PutGrant(ctx, key, hexDigest(data), int64(len(data)), "application/json")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(grant.URL, "X-Amz-Checksum-Sha256=") {
		t.Fatal("upload grant must bind the declared checksum")
	}
	response := send(t, grant, data)
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("upload status=%d", response.StatusCode)
	}
	version := response.Header.Get("x-amz-version-id")
	object, err := store.Commit(ctx, key, version, hexDigest(data), int64(len(data)))
	if err != nil {
		t.Fatal(err)
	}
	if object.Version != version {
		t.Fatalf("committed version=%q reported=%q", object.Version, version)
	}
	download, err := store.GetGrant(ctx, key, version, "application/json")
	if err != nil {
		t.Fatal(err)
	}
	fetched := send(t, download, nil)
	defer fetched.Body.Close()
	body, _ := io.ReadAll(fetched.Body)
	if !bytes.Equal(body, data) {
		t.Fatalf("downloaded %q", body)
	}
	if fetched.Header.Get("Content-Disposition") != "attachment" {
		t.Fatal("sensitive artifacts must not render inline")
	}
}

func TestControlPlanePutCommitsExactObjectVersion(t *testing.T) {
	store, _ := s3Store(t)
	data := []byte("browser bundle")
	key := "workspaces/w/runs/r/browser-bundle.tar"
	object, err := store.Put(context.Background(), key, hexDigest(data), int64(len(data)), "application/x-tar", bytes.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	if object.Version == "" || object.Digest != hexDigest(data) || object.Size != int64(len(data)) {
		t.Fatalf("stored object=%+v", object)
	}
	input, err := store.Open(context.Background(), object.Key, object.Version)
	if err != nil {
		t.Fatal(err)
	}
	defer input.Close()
	got, err := io.ReadAll(input)
	if err != nil || !bytes.Equal(got, data) {
		t.Fatalf("stored bytes=%q err=%v", got, err)
	}
}

func TestCommitRejectsBytesThatDoNotMatchTheDeclaration(t *testing.T) {
	store, fake := s3Store(t)
	ctx := context.Background()
	data := []byte("declared bytes")
	key := "workspaces/w/runs/r/bundle.tar"
	grant, err := store.PutGrant(ctx, key, hexDigest(data), int64(len(data)), "application/x-tar")
	if err != nil {
		t.Fatal(err)
	}
	// A presigned upload cannot bind a checksum the store will enforce, so the
	// declaration is proven when the artifact is committed.
	response := send(t, grant, []byte("replaced bytes"))
	response.Body.Close()
	version := response.Header.Get("x-amz-version-id")
	if _, err := store.Commit(ctx, key, version, hexDigest(data), int64(len(data))); err == nil {
		t.Fatal("committed an object whose bytes do not match the declaration")
	}
	fake.Tamper(key, version, data)
	if _, err := store.Commit(ctx, key, version, hexDigest(data), int64(len(data))); err != nil {
		t.Fatalf("matching bytes rejected: %v", err)
	}
}

func TestCommitRejectsWrongVersionSizeAndDigest(t *testing.T) {
	store, _ := s3Store(t)
	ctx := context.Background()
	data := []byte("committed result")
	key := "workspaces/w/jobs/j/attempts/1/diff_json"
	grant, err := store.PutGrant(ctx, key, hexDigest(data), int64(len(data)), "application/json")
	if err != nil {
		t.Fatal(err)
	}
	response := send(t, grant, data)
	response.Body.Close()
	version := response.Header.Get("x-amz-version-id")
	if _, err := store.Commit(ctx, key, "v999", hexDigest(data), int64(len(data))); err == nil {
		t.Fatal("unknown version committed")
	}
	if _, err := store.Commit(ctx, key, version, hexDigest(data), int64(len(data))+1); err == nil {
		t.Fatal("wrong size committed")
	}
	if _, err := store.Commit(ctx, key, version, hexDigest([]byte("other")), int64(len(data))); err == nil {
		t.Fatal("wrong digest committed")
	}
	if _, err := store.Commit(ctx, key, version, hexDigest(data), int64(len(data))); err != nil {
		t.Fatalf("exact identity rejected: %v", err)
	}
}

func TestCleanupRemovesOnlyExpiredOrphanObjects(t *testing.T) {
	store, fake := s3Store(t)
	ctx := context.Background()
	versions := map[string]string{}
	for _, key := range []string{"kept/result", "orphan/old", "orphan/new"} {
		data := []byte(key)
		grant, err := store.PutGrant(ctx, key, hexDigest(data), int64(len(data)), "application/json")
		if err != nil {
			t.Fatal(err)
		}
		response := send(t, grant, data)
		response.Body.Close()
		versions[key] = response.Header.Get("x-amz-version-id")
	}
	fake.Age("orphan/old", versions["orphan/old"], time.Now().Add(-25*time.Hour))
	removed, err := store.Cleanup(ctx, map[string]bool{"kept/result": true}, time.Now().Add(-24*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if removed != 1 {
		t.Fatalf("removed=%d", removed)
	}
	remaining := map[string]bool{}
	for _, key := range fake.Keys() {
		remaining[key] = true
	}
	if !remaining["kept/result"] || !remaining["orphan/new"] || remaining["orphan/old"] {
		t.Fatalf("remaining=%v", remaining)
	}
}
