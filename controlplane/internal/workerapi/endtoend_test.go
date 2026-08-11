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
	"strings"
	"testing"

	"github.com/amln19/tracewake/controlplane/internal/artifacts"
	"github.com/amln19/tracewake/controlplane/internal/awstest"
	"github.com/amln19/tracewake/controlplane/internal/controlplane"
	"github.com/amln19/tracewake/controlplane/internal/httpapi"
	"github.com/amln19/tracewake/controlplane/internal/store"
	"github.com/amln19/tracewake/controlplane/internal/workerapi"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/jackc/pgx/v5/pgxpool"
)

type deployment struct {
	service     *controlplane.Service
	pool        *pgxpool.Pool
	artifacts   artifacts.Store
	public      *httptest.Server
	private     *httptest.Server
	workspace   string
	token       string
	workerID    string
	workerToken string
}

type browserCredentials struct {
	cookie *http.Cookie
	csrf   string
}

func hexDigest(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// newDeployment starts the public and private APIs over either local storage
// or object storage, so one lifecycle test proves both deployments.
func newDeployment(t *testing.T, hosted bool) *deployment {
	t.Helper()
	databaseURL := os.Getenv("TRACEWAKE_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("TRACEWAKE_TEST_DATABASE_URL is not set")
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
		fake := awstest.NewS3("tracewake-artifacts")
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
	resultSchema, err := os.ReadFile("../../../contracts/schemas/v1/result-envelope.schema.json")
	if err != nil {
		t.Fatal(err)
	}
	workerAPI, err := workerapi.New(service, artifactStore, private.URL, resultSchema)
	if err != nil {
		t.Fatal(err)
	}
	privateMux.Handle("/", workerAPI.Handler())
	workspace, token, err := service.CreateWorkspace(ctx, "end-to-end", []string{"runs:read", "runs:write", "jobs:read", "jobs:write", "artifacts:read", "audit:read"})
	if err != nil {
		t.Fatal(err)
	}
	workerID, workerToken, err := service.CreateWorkerCredential(ctx)
	if err != nil {
		t.Fatal(err)
	}
	return &deployment{
		service: service, pool: database.Pool(), artifacts: artifactStore, public: public, private: private,
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

func (d *deployment) browserSession(t *testing.T, token string) browserCredentials {
	t.Helper()
	raw, err := json.Marshal(map[string]string{"token": token})
	if err != nil {
		t.Fatal(err)
	}
	response, err := http.Post(d.public.URL+"/v1/browser/sessions", "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusCreated {
		body, _ := io.ReadAll(response.Body)
		t.Fatalf("browser session status=%d body=%s", response.StatusCode, body)
	}
	var session map[string]any
	if err := json.NewDecoder(response.Body).Decode(&session); err != nil {
		t.Fatal(err)
	}
	cookies := response.Cookies()
	if len(cookies) != 1 {
		t.Fatalf("browser session cookies=%v", cookies)
	}
	return browserCredentials{cookie: cookies[0], csrf: session["csrf_token"].(string)}
}

func browserCall(t *testing.T, method, url string, credentials browserCredentials, body any, csrf string) (*http.Response, map[string]any) {
	t.Helper()
	var input io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		input = bytes.NewReader(raw)
	}
	request, err := http.NewRequest(method, url, input)
	if err != nil {
		t.Fatal(err)
	}
	request.AddCookie(credentials.cookie)
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	if csrf != "" {
		request.Header.Set("X-Tracewake-CSRF", csrf)
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	decoded := map[string]any{}
	if strings.HasPrefix(response.Header.Get("Content-Type"), "application/json") {
		_ = json.NewDecoder(response.Body).Decode(&decoded)
	}
	return response, decoded
}

func TestBrowserSessionIsShortLivedScopedAndCSRFProtected(t *testing.T) {
	deployed := newDeployment(t, false)
	credentials := deployed.browserSession(t, deployed.token)
	if credentials.cookie.Name != "__Host-tracewake_session" || !credentials.cookie.HttpOnly || !credentials.cookie.Secure || credentials.cookie.SameSite != http.SameSiteStrictMode || credentials.cookie.Path != "/" {
		t.Fatalf("unsafe browser cookie: %+v", credentials.cookie)
	}
	if credentials.cookie.MaxAge < 14*60 || credentials.cookie.MaxAge > 15*60 {
		t.Fatalf("browser cookie max age=%d", credentials.cookie.MaxAge)
	}

	response, _ := browserCall(t, "GET", deployed.public.URL+"/v1/runs", credentials, nil, "")
	response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("session-authenticated run list status=%d", response.StatusCode)
	}

	bundle := []byte("browser bundle bytes")
	response, pending := browserCall(t, "POST", deployed.public.URL+"/v1/browser/runs/uploads", credentials, map[string]any{
		"bundle_format_version": 1,
		"bundle_digest":         hexDigest(bundle),
		"bundle_size":           len(bundle),
	}, credentials.csrf)
	response.Body.Close()
	if response.StatusCode != http.StatusCreated || pending["run_id"] == nil || pending["state"] != "pending" {
		t.Fatalf("browser upload declaration status=%d body=%v", response.StatusCode, pending)
	}
	for _, forbidden := range []string{"upload_url", "upload_headers", "object_key", "object_version"} {
		if _, exposed := pending[forbidden]; exposed {
			t.Fatalf("browser upload exposed %s: %v", forbidden, pending)
		}
	}
	runID := pending["run_id"].(string)
	request, err := http.NewRequest(http.MethodPut, deployed.public.URL+"/v1/browser/runs/uploads/"+runID, bytes.NewReader(bundle))
	if err != nil {
		t.Fatal(err)
	}
	request.AddCookie(credentials.cookie)
	request.Header.Set("Content-Type", "application/x-tar")
	request.Header.Set("X-Tracewake-CSRF", credentials.csrf)
	request.Header.Set("X-Tracewake-Bundle-Digest", hexDigest(bundle))
	request.Header.Set("X-Tracewake-Bundle-Format", "1")
	response, err = http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	var completed map[string]any
	if err := json.NewDecoder(response.Body).Decode(&completed); err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusOK || completed["state"] != "validating" {
		t.Fatalf("browser upload status=%d body=%v", response.StatusCode, completed)
	}

	bearerRequest, err := http.NewRequest(http.MethodPost, deployed.public.URL+"/v1/browser/runs/uploads", strings.NewReader(`{"bundle_format_version":1,"bundle_digest":"`+hexDigest(bundle)+`","bundle_size":20}`))
	if err != nil {
		t.Fatal(err)
	}
	bearerRequest.Header.Set("Authorization", "Bearer "+deployed.token)
	bearerRequest.Header.Set("Content-Type", "application/json")
	bearerRequest.Header.Set("X-Tracewake-CSRF", credentials.csrf)
	response, err = http.DefaultClient.Do(bearerRequest)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusUnauthorized {
		t.Fatalf("browser upload accepted bearer token: %d", response.StatusCode)
	}

	jobID := newID(t)
	response, body := browserCall(t, "POST", deployed.public.URL+"/v1/jobs/"+jobID+"/cancel", credentials, nil, "")
	response.Body.Close()
	if response.StatusCode != http.StatusForbidden || body["error"].(map[string]any)["code"] != "forbidden" {
		t.Fatalf("missing CSRF status=%d body=%v", response.StatusCode, body)
	}
	response, _ = browserCall(t, "POST", deployed.public.URL+"/v1/jobs/"+jobID+"/cancel", credentials, nil, credentials.csrf)
	response.Body.Close()
	if response.StatusCode != http.StatusNotFound {
		t.Fatalf("valid CSRF status=%d", response.StatusCode)
	}

	response, refreshed := browserCall(t, "GET", deployed.public.URL+"/v1/browser/session", credentials, nil, "")
	response.Body.Close()
	if response.StatusCode != http.StatusOK || refreshed["csrf_token"] == credentials.csrf {
		t.Fatalf("refresh status=%d body=%v", response.StatusCode, refreshed)
	}
	newCSRF := refreshed["csrf_token"].(string)
	response, _ = browserCall(t, "POST", deployed.public.URL+"/v1/jobs/"+jobID+"/cancel", credentials, nil, credentials.csrf)
	response.Body.Close()
	if response.StatusCode != http.StatusForbidden {
		t.Fatalf("rotated CSRF remained valid: %d", response.StatusCode)
	}

	ctx := context.Background()
	runID = newID(t)
	_, err = deployed.pool.Exec(ctx, `INSERT INTO runs
        (id,workspace_id,state,declared_bundle_format,declared_bundle_digest,declared_bundle_size,bundle_object_key)
        VALUES($1,$2,'pending',1,$3,1,$4)`, runID, deployed.workspace, hexDigest([]byte(runID)), "workspaces/"+deployed.workspace+"/runs/"+runID+"/bundle.tar")
	if err != nil {
		t.Fatal(err)
	}
	_, otherToken, err := deployed.service.CreateWorkspace(ctx, "other-browser", []string{"runs:read"})
	if err != nil {
		t.Fatal(err)
	}
	other := deployed.browserSession(t, otherToken)
	response, hidden := browserCall(t, "GET", deployed.public.URL+"/v1/runs/"+runID, other, nil, "")
	response.Body.Close()
	if response.StatusCode != http.StatusNotFound || hidden["error"].(map[string]any)["code"] != "not_found" {
		t.Fatalf("cross-tenant run status=%d body=%v", response.StatusCode, hidden)
	}

	response, _ = browserCall(t, "DELETE", deployed.public.URL+"/v1/browser/session", credentials, nil, newCSRF)
	response.Body.Close()
	if response.StatusCode != http.StatusNoContent {
		t.Fatalf("session revocation status=%d", response.StatusCode)
	}
	response, _ = browserCall(t, "GET", deployed.public.URL+"/v1/runs", credentials, nil, "")
	response.Body.Close()
	if response.StatusCode != http.StatusUnauthorized {
		t.Fatalf("revoked session status=%d", response.StatusCode)
	}
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
		version = response.Header.Get("Tracewake-Object-Version")
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
	attemptToken := map[string]string{"Tracewake-Attempt-Token": claim["attempt_token"].(string)}

	oversized, _ := deployed.call(t, "POST", deployed.private.URL+"/internal/v1/jobs/"+jobID+"/attempts/1/artifacts", deployed.workerToken,
		map[string]any{"protocol_version": 1, "attempt_number": 1, "kind": "otlp_json", "media_type": "application/json", "digest": hexDigest([]byte("large")), "size": artifacts.MaxResultSize + 1}, attemptToken)
	if oversized != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversized declaration status=%d", oversized)
	}

	spans := []byte(`{"resourceSpans":[]}`)
	invalidEnvelope := []byte(`{"protocol_version":1,"status":"succeeded"}`)
	mismatchedEnvelope, err := os.ReadFile("../../../contracttest/fixtures/v1/accepted/result-envelope-otlp.json")
	if err != nil {
		t.Fatal(err)
	}
	uploaded := map[string]map[string]any{}
	for kind, body := range map[string][]byte{"otlp_json": spans, "otlp_result_json": invalidEnvelope} {
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
		"object_version": uploaded["otlp_result_json"]["object_version"], "digest": hexDigest(invalidEnvelope),
		"media_type": "application/json", "schema_name": "result-envelope", "size": len(invalidEnvelope), "schema_version": 1,
		"logical_run_digest": "", "bundle_digest": "", "event_count": 0,
		"bundle_format_version": 0, "cassette_format_version": 0, "event_schema_version": 0,
		"companions": []any{map[string]any{
			"artifact_id": newID(t), "kind": "otlp_json", "object_key": uploaded["otlp_json"]["object_key"],
			"object_version": uploaded["otlp_json"]["object_version"], "digest": hexDigest(spans),
			"size": len(spans), "media_type": "application/json", "schema_name": nil, "schema_version": nil,
		}},
	}
	status, rejected := deployed.call(t, "POST", deployed.private.URL+"/internal/v1/jobs/"+jobID+"/attempts/1/complete", deployed.workerToken, completion, attemptToken)
	if status != http.StatusConflict || rejected["error"].(map[string]any)["code"] != "artifact_commit_failed" {
		t.Fatalf("invalid result completion status=%d body=%v", status, rejected)
	}
	status, declaration := deployed.declare(t, jobID, "otlp_result_json", "application/json", mismatchedEnvelope, attemptToken)
	if status != http.StatusCreated {
		t.Fatalf("valid result declaration status=%d body=%v", status, declaration)
	}
	headers, _ := declaration["upload_headers"].(map[string]any)
	stored := transfer(t, declaration["upload_method"].(string), declaration["upload_url"].(string), headers, mismatchedEnvelope)
	stored.Body.Close()
	if stored.StatusCode/100 != 2 {
		t.Fatalf("valid result upload status=%d", stored.StatusCode)
	}
	completion["object_version"] = objectVersion(t, stored)
	completion["digest"] = hexDigest(mismatchedEnvelope)
	completion["size"] = len(mismatchedEnvelope)
	status, rejected = deployed.call(t, "POST", deployed.private.URL+"/internal/v1/jobs/"+jobID+"/attempts/1/complete", deployed.workerToken, completion, attemptToken)
	if status != http.StatusConflict || rejected["error"].(map[string]any)["code"] != "artifact_commit_failed" {
		t.Fatalf("mismatched result completion status=%d body=%v", status, rejected)
	}

	companion := completion["companions"].([]any)[0].(map[string]any)
	var matchingDocument map[string]any
	if err = json.Unmarshal(mismatchedEnvelope, &matchingDocument); err != nil {
		t.Fatal(err)
	}
	result := matchingDocument["result"].(map[string]any)
	result["artifact"] = map[string]any{
		"artifact_id": companion["artifact_id"], "object_key": companion["object_key"],
		"object_version": companion["object_version"], "digest": companion["digest"], "size": companion["size"],
		"media_type": companion["media_type"], "schema_name": nil, "schema_version": nil,
	}
	provenance := result["provenance"].(map[string]any)
	provenance["inputs"] = []any{map[string]any{
		"run_id": run, "logical_run_digest": hexDigest([]byte("logical")), "bundle_digest": hexDigest([]byte("bundle")),
		"bundle_object_key": "workspaces/" + deployed.workspace + "/runs/" + run + "/bundle.tar", "bundle_object_version": "version-1",
		"bundle_format_version": 1, "cassette_format_version": 1, "event_schema_version": 3,
	}}
	envelope, err := json.Marshal(matchingDocument)
	if err != nil {
		t.Fatal(err)
	}
	envelope = append(envelope, '\n')
	status, declaration = deployed.declare(t, jobID, "otlp_result_json", "application/json", envelope, attemptToken)
	if status != http.StatusCreated {
		t.Fatalf("matching result declaration status=%d body=%v", status, declaration)
	}
	headers, _ = declaration["upload_headers"].(map[string]any)
	stored = transfer(t, declaration["upload_method"].(string), declaration["upload_url"].(string), headers, envelope)
	stored.Body.Close()
	if stored.StatusCode/100 != 2 {
		t.Fatalf("matching result upload status=%d", stored.StatusCode)
	}
	completion["object_version"] = objectVersion(t, stored)
	completion["digest"] = hexDigest(envelope)
	completion["size"] = len(envelope)
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

	browser := deployed.browserSession(t, deployed.token)
	request, err := http.NewRequest("GET", deployed.public.URL+"/v1/browser/artifacts/"+registered["otlp_json"], nil)
	if err != nil {
		t.Fatal(err)
	}
	request.AddCookie(browser.cookie)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	browserBytes, _ := io.ReadAll(response.Body)
	response.Body.Close()
	if response.StatusCode != http.StatusOK || !bytes.Equal(browserBytes, spans) {
		t.Fatalf("browser artifact status=%d body=%q", response.StatusCode, browserBytes)
	}
	if !strings.HasPrefix(response.Header.Get("Content-Disposition"), "attachment") || response.Header.Get("X-Content-Type-Options") != "nosniff" {
		t.Fatalf("browser artifact headers=%v", response.Header)
	}
	if bytes.Contains(browserBytes, []byte("download_url")) || bytes.Contains(browserBytes, []byte("object_key")) {
		t.Fatalf("browser artifact exposed storage access: %s", browserBytes)
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
			bundle := []byte("tracewake deterministic bundle bytes")

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
			attemptToken := map[string]string{"Tracewake-Attempt-Token": claim["attempt_token"].(string)}
			inputs := claim["input_artifacts"].([]any)
			input := inputs[0].(map[string]any)
			declared, err := deployed.service.InputArtifact(ctx, jobID, 1, claim["attempt_token"].(string), input["artifact_id"].(string))
			if err != nil {
				t.Fatalf("service input artifact: %v", err)
			}
			if _, err = deployed.artifacts.GetGrant(ctx, declared.ObjectKey, declared.ObjectVersion, declared.MediaType); err != nil {
				t.Fatalf("input grant: key=%q version=%q error=%v", declared.ObjectKey, declared.ObjectVersion, err)
			}

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

			result, err := os.ReadFile("../../../contracttest/fixtures/v1/accepted/result-envelope.json")
			if err != nil {
				t.Fatal(err)
			}
			var resultDocument map[string]any
			if err = json.Unmarshal(result, &resultDocument); err != nil {
				t.Fatal(err)
			}
			validation := resultDocument["result"].(map[string]any)
			validation["run_id"] = runID
			validation["logical_run_digest"] = hexDigest([]byte("logical"))
			validation["bundle_digest"] = hexDigest(bundle)
			validation["provenance"].(map[string]any)["inputs"] = []any{map[string]any{
				"run_id": runID, "logical_run_digest": hexDigest([]byte("logical")), "bundle_digest": hexDigest(bundle),
				"bundle_object_key": "workspaces/" + deployed.workspace + "/runs/" + runID + "/bundle.tar", "bundle_object_version": bundleVersion,
				"bundle_format_version": 1, "cassette_format_version": 1, "event_schema_version": 3,
			}}
			result, err = json.Marshal(resultDocument)
			if err != nil {
				t.Fatal(err)
			}
			result = append(result, '\n')
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

func TestDeletionRemovesATenantRunThroughThePublicAPI(t *testing.T) {
	deployed := newDeployment(t, false)
	ctx := context.Background()
	run := newID(t)
	_, err := deployed.pool.Exec(ctx, `INSERT INTO runs(id,workspace_id,state,declared_bundle_format,declared_bundle_digest,declared_bundle_size,bundle_object_key,bundle_object_version,validated_bundle_format,cassette_format_version,event_schema_version,logical_run_digest,event_count,ready_at)
        VALUES($1,$2,'ready',1,$3,1,$4,'version-1',1,1,3,$5,1,transaction_timestamp())`,
		run, deployed.workspace, hexDigest([]byte("delete-bundle")), "workspaces/"+deployed.workspace+"/runs/"+run+"/bundle.tar", hexDigest([]byte("delete-logical")))
	if err != nil {
		t.Fatal(err)
	}
	if status, _ := deployed.call(t, "GET", deployed.public.URL+"/v1/runs/"+run, deployed.token, nil, nil); status != http.StatusOK {
		t.Fatalf("the run is not readable before deletion: %d", status)
	}

	other, otherToken, err := deployed.service.CreateWorkspace(ctx, "deletion-neighbour", []string{"runs:write"})
	if err != nil {
		t.Fatal(err)
	}
	if status, _ := deployed.call(t, "DELETE", deployed.public.URL+"/v1/runs/"+run, otherToken, nil, nil); status != http.StatusNotFound {
		t.Fatalf("a second workspace deleted another workspace's run: %d", status)
	}
	var state string
	if err := deployed.pool.QueryRow(ctx, "SELECT state FROM runs WHERE id=$1", run).Scan(&state); err != nil || state != "ready" {
		t.Fatalf("a cross-workspace delete changed the run: state=%q err=%v (neighbour %s)", state, err, other)
	}

	if status, _ := deployed.call(t, "DELETE", deployed.public.URL+"/v1/runs/"+run, deployed.token, nil, nil); status != http.StatusNoContent {
		t.Fatalf("deletion status=%d", status)
	}
	if status, _ := deployed.call(t, "GET", deployed.public.URL+"/v1/runs/"+run, deployed.token, nil, nil); status != http.StatusNotFound {
		t.Fatalf("a deleted run is still readable: %d", status)
	}
	status, refused := deployed.call(t, "POST", deployed.public.URL+"/v1/jobs", deployed.token,
		map[string]any{"operation": "otlp", "run_ids": []string{run}}, map[string]string{"Idempotency-Key": "deleted-" + run})
	if status != http.StatusConflict {
		t.Fatalf("a deleted run was accepted for analysis: %d %v", status, refused)
	}
	if status, _ := deployed.call(t, "DELETE", deployed.public.URL+"/v1/runs/"+run, deployed.token, nil, nil); status != http.StatusNotFound {
		t.Fatalf("deleting twice did not report the run as gone: %d", status)
	}
}
