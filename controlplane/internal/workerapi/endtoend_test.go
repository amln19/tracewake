package workerapi_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"

	"github.com/amln19/locus/controlplane/internal/artifacts"
	"github.com/amln19/locus/controlplane/internal/awstest"
	"github.com/amln19/locus/controlplane/internal/controlplane"
	"github.com/amln19/locus/controlplane/internal/httpapi"
	"github.com/amln19/locus/controlplane/internal/store"
	"github.com/amln19/locus/controlplane/internal/workerapi"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/jackc/pgx/v5/pgxpool"
)

type deployment struct {
	service     *controlplane.Service
	pool        *pgxpool.Pool
	public      *httptest.Server
	private     *httptest.Server
	workspace   string
	token       string
	workerID    string
	workerToken string
}

func hexDigest(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// newDeployment starts the public and private APIs over either local storage
// or object storage, so one lifecycle test proves both deployments.
func newDeployment(t *testing.T, hosted bool) *deployment {
	t.Helper()
	databaseURL := os.Getenv("LOCUS_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("LOCUS_TEST_DATABASE_URL is not set")
	}
	ctx := context.Background()
	database, err := store.Open(ctx, databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(database.Close)
	if err := database.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	ring := controlplane.KeyRing{CurrentVersion: 1, Current: []byte("end-to-end-test-pepper-material!!")}
	service, err := controlplane.New(database.Pool(), ring, ring)
	if err != nil {
		t.Fatal(err)
	}
	local, err := artifacts.NewFilesystem(t.TempDir(), []byte("object-url-signing-key-for-tests!"))
	if err != nil {
		t.Fatal(err)
	}
	var artifactStore artifacts.Store = local
	if hosted {
		fake := awstest.NewS3("locus-artifacts")
		t.Cleanup(fake.Close)
		artifactStore, err = artifacts.NewS3(awstest.Config(fake.Server.URL), fake.Bucket, func(options *s3.Options) {
			options.UsePathStyle = true
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	publicMux := http.NewServeMux()
	privateMux := http.NewServeMux()
	if !hosted {
		publicMux.Handle("/objects/", local.Handler())
		privateMux.Handle("/objects/", local.Handler())
	}
	public := httptest.NewServer(publicMux)
	private := httptest.NewServer(privateMux)
	t.Cleanup(public.Close)
	t.Cleanup(private.Close)
	publicMux.Handle("/", httpapi.New(service, artifactStore, public.URL).Handler())
	privateMux.Handle("/", workerapi.New(service, artifactStore, private.URL).Handler())
	workspace, token, err := service.CreateWorkspace(ctx, "end-to-end", []string{"runs:read", "runs:write", "jobs:read", "jobs:write", "artifacts:read", "audit:read"})
	if err != nil {
		t.Fatal(err)
	}
	workerID, workerToken, err := service.CreateWorkerCredential(ctx)
	if err != nil {
		t.Fatal(err)
	}
	return &deployment{
		service: service, pool: database.Pool(), public: public, private: private,
		workspace: workspace, token: token, workerID: workerID, workerToken: workerToken,
	}
}

// notificationFor reads the queued notification this run's mandatory
// validation job created, ignoring unrelated work in the shared test database.
func (d *deployment) notificationFor(t *testing.T, runID string) map[string]any {
	t.Helper()
	var payload map[string]any
	err := d.pool.QueryRow(context.Background(), `SELECT o.payload FROM outbox o
        JOIN jobs j ON j.id=o.aggregate_id JOIN job_inputs i ON i.id=j.input_id
        WHERE i.run_a_id=$1 AND o.topic='job.created' ORDER BY o.id DESC LIMIT 1`, runID).Scan(&payload)
	if err != nil {
		t.Fatal(err)
	}
	return payload
}

func (d *deployment) call(t *testing.T, method, url, token string, body any, headers map[string]string) (int, map[string]any) {
	t.Helper()
	var payload io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		payload = bytes.NewReader(raw)
	}
	request, err := http.NewRequest(method, url, payload)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer "+token)
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	for name, value := range headers {
		request.Header.Set(name, value)
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	raw, _ := io.ReadAll(response.Body)
	decoded := map[string]any{}
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &decoded)
	}
	return response.StatusCode, decoded
}

// transfer uses a grant exactly as a client would: no service credential, only
// the signed URL and the headers the grant names.
func transfer(t *testing.T, method, url string, headers map[string]any, body []byte) *http.Response {
	t.Helper()
	var payload io.Reader
	if body != nil {
		payload = bytes.NewReader(body)
	}
	request, err := http.NewRequest(method, url, payload)
	if err != nil {
		t.Fatal(err)
	}
	for name, value := range headers {
		if text, ok := value.(string); ok {
			request.Header.Set(name, text)
		}
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	return response
}

func objectVersion(t *testing.T, response *http.Response) string {
	t.Helper()
	version := response.Header.Get("x-amz-version-id")
	if version == "" {
		version = response.Header.Get("Locus-Object-Version")
	}
	if version == "" {
		t.Fatal("object store reported no immutable version")
	}
	return version
}

func TestHostedRoundTripCommitsExactArtifactIdentity(t *testing.T) {
	for _, hosted := range []bool{false, true} {
		name := "filesystem"
		if hosted {
			name = "object storage"
		}
		t.Run(name, func(t *testing.T) {
			deployed := newDeployment(t, hosted)
			ctx := context.Background()
			bundle := []byte("locus deterministic bundle bytes")

			status, grant := deployed.call(t, "POST", deployed.public.URL+"/v1/runs/uploads", deployed.token,
				map[string]any{"bundle_format_version": 1, "bundle_digest": hexDigest(bundle), "bundle_size": len(bundle)}, nil)
			if status != http.StatusCreated {
				t.Fatalf("upload declaration status=%d body=%v", status, grant)
			}
			runID, _ := grant["run_id"].(string)
			upload := transfer(t, "PUT", grant["upload_url"].(string), nil, bundle)
			upload.Body.Close()
			if upload.StatusCode/100 != 2 {
				t.Fatalf("bundle upload status=%d", upload.StatusCode)
			}
			bundleVersion := objectVersion(t, upload)

			status, _ = deployed.call(t, "POST", deployed.public.URL+"/v1/runs/uploads/"+runID+"/complete", deployed.token,
				map[string]any{"object_version": bundleVersion, "digest": hexDigest(bundle), "size": len(bundle)}, nil)
			if status != http.StatusOK {
				t.Fatalf("upload completion status=%d", status)
			}
			status, _ = deployed.call(t, "POST", deployed.public.URL+"/v1/runs/uploads/"+runID+"/complete", deployed.token,
				map[string]any{"object_version": bundleVersion, "digest": hexDigest(bundle), "size": len(bundle)}, nil)
			if status != http.StatusOK {
				t.Fatalf("repeated completion status=%d", status)
			}
			status, _ = deployed.call(t, "POST", deployed.public.URL+"/v1/runs/uploads/"+runID+"/complete", deployed.token,
				map[string]any{"object_version": "forged-version", "digest": hexDigest(bundle), "size": len(bundle)}, nil)
			if status != http.StatusConflict {
				t.Fatalf("changed object identity status=%d", status)
			}

			status, claim := deployed.call(t, "POST", deployed.private.URL+"/internal/v1/claims", deployed.workerToken,
				map[string]any{"protocol_version": 1, "worker_id": deployed.workerID, "notification": deployed.notificationFor(t, runID)}, nil)
			if status != http.StatusCreated {
				t.Fatalf("claim status=%d body=%v", status, claim)
			}
			jobID := claim["job_id"].(string)
			attemptToken := map[string]string{"Locus-Attempt-Token": claim["attempt_token"].(string)}
			inputs := claim["input_artifacts"].([]any)
			input := inputs[0].(map[string]any)

			status, reference := deployed.call(t, "GET", deployed.private.URL+"/internal/v1/jobs/"+jobID+"/attempts/1/inputs/"+input["artifact_id"].(string), deployed.workerToken, nil, attemptToken)
			if status != http.StatusOK {
				t.Fatalf("input reference status=%d", status)
			}
			download := transfer(t, "GET", reference["download_url"].(string), nil, nil)
			fetched, _ := io.ReadAll(download.Body)
			download.Body.Close()
			if !bytes.Equal(fetched, bundle) {
				t.Fatalf("worker downloaded %q", fetched)
			}

			result := []byte(`{"protocol_version":1,"status":"succeeded"}`)
			status, declaration := deployed.call(t, "POST", deployed.private.URL+"/internal/v1/jobs/"+jobID+"/attempts/1/artifacts", deployed.workerToken,
				map[string]any{"protocol_version": 1, "attempt_number": 1, "kind": "validation_json", "media_type": "application/json", "digest": hexDigest(result), "size": len(result)}, attemptToken)
			if status != http.StatusCreated {
				t.Fatalf("artifact declaration status=%d body=%v", status, declaration)
			}
			headers, _ := declaration["upload_headers"].(map[string]any)
			stored := transfer(t, declaration["upload_method"].(string), declaration["upload_url"].(string), headers, result)
			stored.Body.Close()
			if stored.StatusCode/100 != 2 {
				t.Fatalf("result upload status=%d", stored.StatusCode)
			}
			resultVersion := objectVersion(t, stored)

			completion := map[string]any{
				"artifact_id": nil, "kind": "validation_json", "object_key": declaration["object_key"],
				"object_version": resultVersion, "digest": hexDigest(result), "media_type": "application/json",
				"schema_name": "result-envelope", "size": len(result), "schema_version": 1,
				"logical_run_digest": hexDigest([]byte("logical")), "bundle_digest": hexDigest(bundle),
				"event_count": 1, "bundle_format_version": 1, "cassette_format_version": 1, "event_schema_version": 3,
				"companions": []any{},
			}
			completion["artifact_id"] = ""
			status, _ = deployed.call(t, "POST", deployed.private.URL+"/internal/v1/jobs/"+jobID+"/attempts/1/complete", deployed.workerToken, completion, attemptToken)
			if status != http.StatusOK {
				t.Fatalf("completion status=%d", status)
			}

			var storedKey, storedVersion string
			if err := deployed.pool.QueryRow(ctx, `SELECT a.object_key,a.object_version FROM artifacts a JOIN jobs j ON j.result_artifact_id=a.id WHERE j.id=$1`, jobID).Scan(&storedKey, &storedVersion); err != nil {
				t.Fatal(err)
			}
			if storedVersion != resultVersion || storedKey != declaration["object_key"] {
				t.Fatalf("committed identity key=%q version=%q", storedKey, storedVersion)
			}

			status, run := deployed.call(t, "GET", deployed.public.URL+"/v1/runs/"+runID, deployed.token, nil, nil)
			if status != http.StatusOK || run["state"] != "ready" {
				t.Fatalf("run state=%v status=%d", run["state"], status)
			}

			_, other, err := deployed.service.CreateWorkspace(ctx, "other", []string{"runs:read", "artifacts:read"})
			if err != nil {
				t.Fatal(err)
			}
			if status, _ := deployed.call(t, "GET", deployed.public.URL+"/v1/runs/"+runID, other, nil, nil); status != http.StatusNotFound {
				t.Fatalf("cross-workspace run read status=%d", status)
			}
			var artifactID string
			if err := deployed.pool.QueryRow(ctx, "SELECT result_artifact_id FROM jobs WHERE id=$1", jobID).Scan(&artifactID); err != nil {
				t.Fatal(err)
			}
			if status, _ := deployed.call(t, "GET", deployed.public.URL+"/v1/artifacts/"+artifactID+"/download", other, nil, nil); status != http.StatusNotFound {
				t.Fatalf("cross-workspace artifact download status=%d", status)
			}
		})
	}
}
