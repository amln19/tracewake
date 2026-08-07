package workerapi_test

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
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

// declare asks for an attempt-scoped object key the way a worker does.
func (d *deployment) declare(t *testing.T, jobID, kind, mediaType string, body []byte, headers map[string]string) (int, map[string]any) {
	t.Helper()
	return d.call(t, "POST", d.private.URL+"/internal/v1/jobs/"+jobID+"/attempts/1/artifacts", d.workerToken,
		map[string]any{"protocol_version": 1, "attempt_number": 1, "kind": kind, "media_type": mediaType, "digest": hexDigest(body), "size": len(body)}, headers)
}

func TestSingleRunAnalysisCommitsItsResultAndCompanion(t *testing.T) {
	deployed := newDeployment(t, false)
	ctx := context.Background()
	run := newID(t)
	_, err := deployed.pool.Exec(ctx, `INSERT INTO runs(id,workspace_id,state,declared_bundle_format,declared_bundle_digest,declared_bundle_size,bundle_object_key,bundle_object_version,validated_bundle_format,cassette_format_version,event_schema_version,logical_run_digest,event_count,ready_at)
        VALUES($1,$2,'ready',1,$3,1,$4,'version-1',1,1,3,$5,1,transaction_timestamp())`,
		run, deployed.workspace, hexDigest([]byte("bundle")), "workspaces/"+deployed.workspace+"/runs/"+run+"/bundle.tar", hexDigest([]byte("logical")))
	if err != nil {
		t.Fatal(err)
	}

	key := map[string]string{"Idempotency-Key": "otlp-" + run}
	status, created := deployed.call(t, "POST", deployed.public.URL+"/v1/jobs", deployed.token, map[string]any{"operation": "otlp", "run_ids": []string{run}}, key)
	if status != http.StatusCreated {
		t.Fatalf("job creation status=%d body=%v", status, created)
	}
	jobID := created["job_id"].(string)
	status, replayed := deployed.call(t, "POST", deployed.public.URL+"/v1/jobs", deployed.token, map[string]any{"operation": "otlp", "run_ids": []string{run}}, key)
	if status != http.StatusOK || replayed["job_id"] != jobID {
		t.Fatalf("idempotent replay status=%d body=%v", status, replayed)
	}

	// An analysis profile this deployment does not implement is refused
	// outright rather than quietly replaced with the one it does.
	for _, request := range []map[string]any{
		{"operation": "otlp", "run_ids": []string{run}, "profile": "lexical-v1"},
		{"operation": "diff", "run_ids": []string{run, run}, "profile": "mlx-community/bge-small-en-v1.5-bf16"},
		{"operation": "flamegraph", "run_ids": []string{run}},
	} {
		status, body := deployed.call(t, "POST", deployed.public.URL+"/v1/jobs", deployed.token, request, map[string]string{"Idempotency-Key": "unsupported-" + run})
		if status != http.StatusUnprocessableEntity || body["error"].(map[string]any)["code"] != "unsupported_version" {
			t.Fatalf("unsupported request %v status=%d body=%v", request, status, body)
		}
	}

	status, claim := deployed.call(t, "POST", deployed.private.URL+"/internal/v1/claims", deployed.workerToken,
		map[string]any{"protocol_version": 1, "worker_id": deployed.workerID, "notification": map[string]any{"protocol_version": 1, "job_id": jobID, "job_version": 1, "operation": "otlp"}}, nil)
	if status != http.StatusCreated {
		t.Fatalf("claim status=%d body=%v", status, claim)
	}
	attemptToken := map[string]string{"Locus-Attempt-Token": claim["attempt_token"].(string)}

	oversized, _ := deployed.call(t, "POST", deployed.private.URL+"/internal/v1/jobs/"+jobID+"/attempts/1/artifacts", deployed.workerToken,
		map[string]any{"protocol_version": 1, "attempt_number": 1, "kind": "otlp_json", "media_type": "application/json", "digest": hexDigest([]byte("large")), "size": artifacts.MaxResultSize + 1}, attemptToken)
	if oversized != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversized declaration status=%d", oversized)
	}

	spans := []byte(`{"resourceSpans":[]}`)
	envelope := []byte(`{"protocol_version":1,"status":"succeeded"}`)
	uploaded := map[string]map[string]any{}
	for kind, body := range map[string][]byte{"otlp_json": spans, "otlp_result_json": envelope} {
		status, declaration := deployed.declare(t, jobID, kind, "application/json", body, attemptToken)
		if status != http.StatusCreated {
			t.Fatalf("%s declaration status=%d body=%v", kind, status, declaration)
		}
		headers, _ := declaration["upload_headers"].(map[string]any)
		stored := transfer(t, declaration["upload_method"].(string), declaration["upload_url"].(string), headers, body)
		stored.Body.Close()
		if stored.StatusCode/100 != 2 {
			t.Fatalf("%s upload status=%d", kind, stored.StatusCode)
		}
		uploaded[kind] = map[string]any{"object_key": declaration["object_key"], "object_version": objectVersion(t, stored), "digest": hexDigest(body), "size": len(body)}
	}

	completion := map[string]any{
		"artifact_id": "", "kind": "otlp_result_json", "object_key": uploaded["otlp_result_json"]["object_key"],
		"object_version": uploaded["otlp_result_json"]["object_version"], "digest": hexDigest(envelope),
		"media_type": "application/json", "schema_name": "result-envelope", "size": len(envelope), "schema_version": 1,
		"logical_run_digest": "", "bundle_digest": "", "event_count": 0,
		"bundle_format_version": 0, "cassette_format_version": 0, "event_schema_version": 0,
		"companions": []any{map[string]any{
			"artifact_id": newID(t), "kind": "otlp_json", "object_key": uploaded["otlp_json"]["object_key"],
			"object_version": uploaded["otlp_json"]["object_version"], "digest": hexDigest(spans),
			"size": len(spans), "media_type": "application/json", "schema_name": nil, "schema_version": nil,
		}},
	}
	status, completed := deployed.call(t, "POST", deployed.private.URL+"/internal/v1/jobs/"+jobID+"/attempts/1/complete", deployed.workerToken, completion, attemptToken)
	if status != http.StatusOK {
		t.Fatalf("completion status=%d body=%v", status, completed)
	}

	status, view := deployed.call(t, "GET", deployed.public.URL+"/v1/jobs/"+jobID, deployed.token, nil, nil)
	if status != http.StatusOK || view["state"] != "succeeded" {
		t.Fatalf("job view status=%d state=%v", status, view["state"])
	}
	registered := map[string]string{}
	for _, value := range view["artifacts"].([]any) {
		artifact := value.(map[string]any)
		registered[artifact["kind"].(string)] = artifact["artifact_id"].(string)
	}
	if len(registered) != 2 || registered["otlp_result_json"] == "" || registered["otlp_json"] == "" {
		t.Fatalf("registered artifacts=%v", registered)
	}

	// The companion has to come back byte for byte, or the recorded digest
	// proves nothing about what a tenant can download.
	status, download := deployed.call(t, "GET", deployed.public.URL+"/v1/artifacts/"+registered["otlp_json"]+"/download", deployed.token, nil, nil)
	if status != http.StatusOK {
		t.Fatalf("download status=%d", status)
	}
	fetched := transfer(t, "GET", download["download_url"].(string), nil, nil)
	body, _ := io.ReadAll(fetched.Body)
	fetched.Body.Close()
	if !bytes.Equal(body, spans) || hexDigest(body) != download["digest"] {
		t.Fatalf("downloaded %q with digest %v", body, download["digest"])
	}
}

func newID(t *testing.T) string {
	t.Helper()
	var value [16]byte
	if _, err := rand.Read(value[:]); err != nil {
		t.Fatal(err)
	}
	value[6] = value[6]&0x0f | 0x40
	value[8] = value[8]&0x3f | 0x80
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x", value[:4], value[4:6], value[6:8], value[8:10], value[10:])
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
