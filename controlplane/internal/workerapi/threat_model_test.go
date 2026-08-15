// Tests for claims contracts/threat-model-v1.md makes but nothing else checks.
//
// Most of that document is already pinned by tests written for other reasons —
// fencing by the operations tests, telemetry by the evidence harness, browser
// session shape by the end-to-end suite. What was missing is any test a reader
// can follow back to the sentence it defends, for the claims no other test
// happens to cover. Each test below quotes the claim it exists for, so a claim
// that stops being true fails here rather than waiting to be read.
package workerapi_test

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"
)

// "Attempt tokens constrain mutation to the current claim."
//
// Fencing covers a stale attempt on its own job. This covers the other
// direction: a live, valid token proving a live, valid claim on a different
// job. Every worker endpoint has to judge the token against the job in its own
// path, not merely accept a token that verifies against something.
func TestAttemptTokensConstrainMutationToTheCurrentClaim(t *testing.T) {
	deployed := newDeployment(t, false)
	ctx := context.Background()

	claimJob := func(suffix string) (string, string) {
		t.Helper()
		run := newID(t)
		_, err := deployed.pool.Exec(ctx, `INSERT INTO runs(id,workspace_id,state,declared_bundle_format,declared_bundle_digest,declared_bundle_size,bundle_object_key,bundle_object_version,validated_bundle_format,cassette_format_version,event_schema_version,logical_run_digest,event_count,ready_at)
            VALUES($1,$2,'ready',1,$3,1,$4,'version-1',1,1,3,$5,1,transaction_timestamp())`,
			run, deployed.workspace, hexDigest([]byte("bundle"+suffix)), "workspaces/"+deployed.workspace+"/runs/"+run+"/bundle.tar", hexDigest([]byte("logical"+suffix)))
		if err != nil {
			t.Fatal(err)
		}
		status, created := deployed.call(t, "POST", deployed.public.URL+"/v1/jobs", deployed.token,
			map[string]any{"operation": "otlp", "run_ids": []string{run}}, map[string]string{"Idempotency-Key": "claim-" + suffix + "-" + run})
		if status != http.StatusCreated {
			t.Fatalf("job creation status=%d body=%v", status, created)
		}
		jobID := created["job_id"].(string)
		status, claim := deployed.call(t, "POST", deployed.private.URL+"/internal/v1/claims", deployed.workerToken,
			map[string]any{"protocol_version": 1, "worker_id": deployed.workerID, "notification": map[string]any{"protocol_version": 1, "job_id": jobID, "job_version": 1, "operation": "otlp"}}, nil)
		if status != http.StatusCreated {
			t.Fatalf("claim status=%d body=%v", status, claim)
		}
		return jobID, claim["attempt_token"].(string)
	}

	_, tokenA := claimJob("a")
	jobB, tokenB := claimJob("b")
	if tokenA == tokenB {
		t.Fatal("two claims minted the same attempt token")
	}
	// Job B has a live attempt of its own, so the only thing wrong with these
	// requests is which claim the token belongs to.
	borrowed := map[string]string{"Tracewake-Attempt-Token": tokenA}
	base := deployed.private.URL + "/internal/v1/jobs/" + jobB + "/attempts/1"
	for _, attempt := range []struct {
		name, method, url string
		body              any
	}{
		{"heartbeat", "PUT", base + "/heartbeat", map[string]any{"protocol_version": 1, "attempt_number": 1, "observed_lease_expires_at": "1970-01-01T00:00:00Z"}},
		{"progress", "PUT", base + "/progress", map[string]any{"protocol_version": 1, "attempt_number": 1, "sequence": 1, "Stage": "analyzing", "Message": "borrowed"}},
		{"cancellation", "GET", base + "/cancellation", nil},
		{"declare", "POST", base + "/artifacts", map[string]any{"protocol_version": 1, "attempt_number": 1, "kind": "otlp_json", "media_type": "application/json", "digest": hexDigest([]byte("x")), "size": 1}},
		{"fail", "POST", base + "/fail", map[string]any{"schema_version": 1, "Code": "internal", "Message": "borrowed", "Retryable": true}},
	} {
		t.Run(attempt.name, func(t *testing.T) {
			status, body := deployed.call(t, attempt.method, attempt.url, deployed.workerToken, attempt.body, borrowed)
			if status != http.StatusConflict {
				t.Fatalf("another claim's attempt token was accepted: status=%d body=%v", status, body)
			}
		})
	}
	// The borrowed token must also not have moved job B along.
	var state string
	if err := deployed.pool.QueryRow(ctx, "SELECT state FROM jobs WHERE id=$1", jobB).Scan(&state); err != nil {
		t.Fatal(err)
	}
	if state != "running" {
		t.Fatalf("another claim's attempt token changed job B state to %q", state)
	}
}

// "a request naming another workspace's run is indistinguishable from a request
// naming one that does not exist, and changes nothing" — and the same for the
// jobs built from them.
func TestAnotherWorkspacesIdentifiersAreIndistinguishableFromUnknownOnes(t *testing.T) {
	deployed := newDeployment(t, false)
	ctx := context.Background()
	run := newID(t)
	_, err := deployed.pool.Exec(ctx, `INSERT INTO runs(id,workspace_id,state,declared_bundle_format,declared_bundle_digest,declared_bundle_size,bundle_object_key,bundle_object_version,validated_bundle_format,cassette_format_version,event_schema_version,logical_run_digest,event_count,ready_at)
        VALUES($1,$2,'ready',1,$3,1,$4,'version-1',1,1,3,$5,1,transaction_timestamp())`,
		run, deployed.workspace, hexDigest([]byte("bundle")), "workspaces/"+deployed.workspace+"/runs/"+run+"/bundle.tar", hexDigest([]byte("logical")))
	if err != nil {
		t.Fatal(err)
	}
	status, created := deployed.call(t, "POST", deployed.public.URL+"/v1/jobs", deployed.token,
		map[string]any{"operation": "otlp", "run_ids": []string{run}}, map[string]string{"Idempotency-Key": "owned-" + run})
	if status != http.StatusCreated {
		t.Fatalf("job creation status=%d body=%v", status, created)
	}
	ownedJob := created["job_id"].(string)

	_, neighbour, err := deployed.service.CreateWorkspace(ctx, "neighbour", []string{"runs:read", "jobs:read", "jobs:write"})
	if err != nil {
		t.Fatal(err)
	}
	unknownRun, unknownJob := newID(t), newID(t)

	answer := func(method, url string, body any, key string) string {
		t.Helper()
		var headers map[string]string
		if key != "" {
			headers = map[string]string{"Idempotency-Key": key}
		}
		status, decoded := deployed.call(t, method, url, neighbour, body, headers)
		code := ""
		if failure, ok := decoded["error"].(map[string]any); ok {
			code, _ = failure["code"].(string)
		}
		return fmt.Sprintf("%s %d %s", method, status, code)
	}

	for _, pair := range []struct {
		what             string
		foreign, unknown string
	}{
		{"read a run", deployed.public.URL + "/v1/runs/" + run, deployed.public.URL + "/v1/runs/" + unknownRun},
		{"read a job", deployed.public.URL + "/v1/jobs/" + ownedJob, deployed.public.URL + "/v1/jobs/" + unknownJob},
		{"delete a run", deployed.public.URL + "/v1/runs/" + run, deployed.public.URL + "/v1/runs/" + unknownRun},
	} {
		method := "GET"
		if strings.HasPrefix(pair.what, "delete") {
			method = "DELETE"
		}
		foreign, unknown := answer(method, pair.foreign, nil, ""), answer(method, pair.unknown, nil, "")
		if foreign != unknown {
			t.Fatalf("%s: another workspace's identifier answered %q but an unknown one answered %q", pair.what, foreign, unknown)
		}
	}

	// Analysing a run it cannot see must be refused the same way as analysing
	// one that was never uploaded.
	foreign := answer("POST", deployed.public.URL+"/v1/jobs", map[string]any{"operation": "otlp", "run_ids": []string{run}}, "foreign-"+run)
	unknown := answer("POST", deployed.public.URL+"/v1/jobs", map[string]any{"operation": "otlp", "run_ids": []string{unknownRun}}, "unknown-"+run)
	if foreign != unknown {
		t.Fatalf("analysing another workspace's run answered %q but an unknown run answered %q", foreign, unknown)
	}

	// "and changes nothing"
	var state string
	if err := deployed.pool.QueryRow(ctx, "SELECT state FROM runs WHERE id=$1", run).Scan(&state); err != nil || state != "ready" {
		t.Fatalf("a neighbouring workspace changed the run: state=%q err=%v", state, err)
	}
}

// "Full tokens, verifiers, and peppers are absent from logs and audit."
func TestAuditRecordsCarryNoTokenMaterial(t *testing.T) {
	deployed := newDeployment(t, false)
	ctx := context.Background()
	run := newID(t)
	_, err := deployed.pool.Exec(ctx, `INSERT INTO runs(id,workspace_id,state,declared_bundle_format,declared_bundle_digest,declared_bundle_size,bundle_object_key,bundle_object_version,validated_bundle_format,cassette_format_version,event_schema_version,logical_run_digest,event_count,ready_at)
        VALUES($1,$2,'ready',1,$3,1,$4,'version-1',1,1,3,$5,1,transaction_timestamp())`,
		run, deployed.workspace, hexDigest([]byte("bundle")), "workspaces/"+deployed.workspace+"/runs/"+run+"/bundle.tar", hexDigest([]byte("logical")))
	if err != nil {
		t.Fatal(err)
	}
	// Exercise the paths that write audit rows: a browser session, a job, a
	// claim, and a terminal failure.
	credentials := deployed.browserSession(t, deployed.token)
	status, created := deployed.call(t, "POST", deployed.public.URL+"/v1/jobs", deployed.token,
		map[string]any{"operation": "otlp", "run_ids": []string{run}}, map[string]string{"Idempotency-Key": "audit-" + run})
	if status != http.StatusCreated {
		t.Fatalf("job creation status=%d body=%v", status, created)
	}
	jobID := created["job_id"].(string)
	status, claim := deployed.call(t, "POST", deployed.private.URL+"/internal/v1/claims", deployed.workerToken,
		map[string]any{"protocol_version": 1, "worker_id": deployed.workerID, "notification": map[string]any{"protocol_version": 1, "job_id": jobID, "job_version": 1, "operation": "otlp"}}, nil)
	if status != http.StatusCreated {
		t.Fatalf("claim status=%d body=%v", status, claim)
	}
	attemptToken := claim["attempt_token"].(string)
	deployed.call(t, "POST", deployed.private.URL+"/internal/v1/jobs/"+jobID+"/attempts/1/fail", deployed.workerToken,
		map[string]any{"schema_version": 1, "Code": "invalid_bundle", "Message": "audit probe", "Retryable": false},
		map[string]string{"Tracewake-Attempt-Token": attemptToken})

	rows, err := deployed.pool.Query(ctx, `SELECT coalesce(actor_id::text,''),event_type,coalesce(payload::text,'') FROM audit_records WHERE workspace_id=$1`, deployed.workspace)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var ledger strings.Builder
	records := 0
	for rows.Next() {
		var actor, event, payload string
		if err := rows.Scan(&actor, &event, &payload); err != nil {
			t.Fatal(err)
		}
		ledger.WriteString(actor + " " + event + " " + payload + "\n")
		records++
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	if records == 0 {
		t.Fatal("the lifecycle wrote no audit records, so this proves nothing")
	}
	for _, secret := range []struct{ name, value string }{
		{"the workspace token", deployed.token},
		{"the worker credential", deployed.workerToken},
		{"the attempt token", attemptToken},
		{"the browser session cookie", credentials.cookie.Value},
		{"the browser CSRF token", credentials.csrf},
	} {
		if strings.Contains(ledger.String(), secret.value) {
			t.Fatalf("%s reached the audit ledger", secret.name)
		}
	}
}

// "Result object keys, versions, buckets, and signed storage URLs are absent
// from the browser surface."
func TestTheBrowserSurfaceCarriesNoObjectKeysOrSignedURLs(t *testing.T) {
	deployed := newDeployment(t, false)
	ctx := context.Background()
	run := newID(t)
	objectKey := "workspaces/" + deployed.workspace + "/runs/" + run + "/bundle.tar"
	_, err := deployed.pool.Exec(ctx, `INSERT INTO runs(id,workspace_id,state,declared_bundle_format,declared_bundle_digest,declared_bundle_size,bundle_object_key,bundle_object_version,validated_bundle_format,cassette_format_version,event_schema_version,logical_run_digest,event_count,ready_at)
        VALUES($1,$2,'ready',1,$3,1,$4,'version-1',1,1,3,$5,1,transaction_timestamp())`,
		run, deployed.workspace, hexDigest([]byte("bundle")), objectKey, hexDigest([]byte("logical")))
	if err != nil {
		t.Fatal(err)
	}
	status, created := deployed.call(t, "POST", deployed.public.URL+"/v1/jobs", deployed.token,
		map[string]any{"operation": "otlp", "run_ids": []string{run}}, map[string]string{"Idempotency-Key": "surface-" + run})
	if status != http.StatusCreated {
		t.Fatalf("job creation status=%d body=%v", status, created)
	}
	jobID := created["job_id"].(string)
	// The artifact row hangs off an attempt, so the job has to be claimed before
	// one can exist.
	status, claim := deployed.call(t, "POST", deployed.private.URL+"/internal/v1/claims", deployed.workerToken,
		map[string]any{"protocol_version": 1, "worker_id": deployed.workerID, "notification": map[string]any{"protocol_version": 1, "job_id": jobID, "job_version": 1, "operation": "otlp"}}, nil)
	if status != http.StatusCreated {
		t.Fatalf("claim status=%d body=%v", status, claim)
	}
	// An artifact whose object identity exists in the database is the only way
	// this test can show the surface withholds it rather than lacking it.
	artifactKey := "workspaces/" + deployed.workspace + "/jobs/" + jobID + "/attempts/1/otlp_result_json"
	if _, err := deployed.pool.Exec(ctx, `INSERT INTO artifacts(id,workspace_id,job_id,attempt_number,kind,object_key,object_version,digest,size,media_type,schema_name,schema_version,authoritative,retention_expires_at)
        VALUES($1,$2,$3,1,'otlp_result_json',$4,'version-7',$5,3,'application/json','result-envelope',1,true,transaction_timestamp()+interval '90 days')`,
		newID(t), deployed.workspace, jobID, artifactKey, hexDigest([]byte("res"))); err != nil {
		t.Fatal(err)
	}

	credentials := deployed.browserSession(t, deployed.token)
	read := func(path string) string {
		t.Helper()
		request, err := http.NewRequest("GET", deployed.public.URL+path, nil)
		if err != nil {
			t.Fatal(err)
		}
		request.AddCookie(credentials.cookie)
		response, err := http.DefaultClient.Do(request)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		raw, _ := io.ReadAll(response.Body)
		if response.StatusCode != http.StatusOK {
			t.Fatalf("GET %s status=%d body=%s", path, response.StatusCode, raw)
		}
		return string(raw)
	}

	session, err := json.Marshal(map[string]any{"csrf": credentials.csrf})
	if err != nil {
		t.Fatal(err)
	}
	surface := strings.Join([]string{
		string(session),
		read("/v1/runs?limit=100"),
		read("/v1/runs/" + run),
		read("/v1/jobs/" + jobID),
		read("/v1/audit?limit=100"),
	}, "\n")

	for _, leak := range []struct{ name, needle string }{
		{"a bundle object key", objectKey},
		{"a result object key", artifactKey},
		{"an object key prefix", "workspaces/"},
		{"an object version", "version-7"},
		{"an object key field", "object_key"},
		{"a signed storage URL", "signature="},
	} {
		if strings.Contains(surface, leak.needle) {
			t.Fatalf("%s reached the browser surface", leak.name)
		}
	}
}
