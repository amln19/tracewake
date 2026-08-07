package controlplane_test

import (
	"context"
	"errors"
	"os"
	"testing"

	"github.com/amln19/locus/controlplane/internal/controlplane"
	"github.com/amln19/locus/controlplane/internal/store"
	"github.com/jackc/pgx/v5/pgxpool"
)

// operations exercises every hosted analysis, including the mandatory
// ingestion job, against one lifecycle so no operation gets a weaker one.
type operationCase struct {
	name      string
	kind      string
	companion string
	media     string
}

func operations() []operationCase {
	return []operationCase{
		{name: "validate", kind: "validation_json"},
		{name: "diff", kind: "diff_json", companion: "diff_html", media: "text/html; charset=utf-8"},
		{name: "otlp", kind: "otlp_result_json", companion: "otlp_json", media: "application/json"},
		{name: "pprof", kind: "pprof_result_json", companion: "pprof", media: "application/octet-stream"},
	}
}

type fixture struct {
	service   *controlplane.Service
	pool      *pgxpool.Pool
	principal controlplane.Principal
	workspace string
	worker    string
}

func newFixture(t *testing.T) *fixture {
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
	ring := controlplane.KeyRing{CurrentVersion: 1, Current: []byte("operation-test-pepper-material!!")}
	service, err := controlplane.New(database.Pool(), ring, ring)
	if err != nil {
		t.Fatal(err)
	}
	workspace, token, err := service.CreateWorkspace(ctx, "operations", []string{"runs:read", "runs:write", "jobs:read", "jobs:write"})
	if err != nil {
		t.Fatal(err)
	}
	principal, err := service.Authenticate(ctx, token, "jobs:write")
	if err != nil {
		t.Fatal(err)
	}
	worker, _, err := service.CreateWorkerCredential(ctx)
	if err != nil {
		t.Fatal(err)
	}
	return &fixture{service: service, pool: database.Pool(), principal: principal, workspace: workspace, worker: worker}
}

func (f *fixture) readyRun(t *testing.T) string {
	t.Helper()
	run := testID(t)
	_, err := f.pool.Exec(context.Background(), `INSERT INTO runs(id,workspace_id,state,declared_bundle_format,declared_bundle_digest,declared_bundle_size,bundle_object_key,bundle_object_version,validated_bundle_format,cassette_format_version,event_schema_version,logical_run_digest,event_count,ready_at)
        VALUES($1,$2,'ready',1,$3,1,$4,$5,1,1,3,$6,1,transaction_timestamp())`,
		run, f.workspace, digest("bundle-"+run), "workspaces/"+f.workspace+"/runs/"+run+"/bundle.tar", digest("version-"+run), digest("logical-"+run))
	if err != nil {
		t.Fatal(err)
	}
	return run
}

// job creates one job of the given operation. Mandatory validation is not a
// public operation, so it is created the way an upload creates it.
func (f *fixture) job(t *testing.T, operation, key string) (string, string) {
	t.Helper()
	ctx := context.Background()
	if operation == "validate" {
		bundleDigest := digest("bundle-" + key)
		upload, err := f.service.CreateUpload(ctx, f.principal, bundleDigest, 1)
		if err != nil {
			t.Fatal(err)
		}
		if err := f.service.CompleteUpload(ctx, f.principal, upload.RunID, digest("version-"+key), bundleDigest, 1); err != nil {
			t.Fatal(err)
		}
		var jobID string
		if err := f.pool.QueryRow(ctx, `SELECT j.id FROM jobs j JOIN job_inputs i ON i.id=j.input_id WHERE i.run_a_id=$1`, upload.RunID).Scan(&jobID); err != nil {
			t.Fatal(err)
		}
		return jobID, bundleDigest
	}
	request := controlplane.JobRequest{Operation: operation, RunIDs: []string{f.readyRun(t)}}
	if operation == "diff" {
		profile := "lexical-v1"
		request.RunIDs = append(request.RunIDs, f.readyRun(t))
		request.Profile = &profile
	}
	job, _, err := f.service.CreateJob(ctx, f.principal, key, request)
	if err != nil {
		t.Fatal(err)
	}
	return job.ID, ""
}

func (f *fixture) completion(t *testing.T, jobID string, attempt int, operation operationCase, bundleDigest string) controlplane.Completion {
	t.Helper()
	prefix := "workspaces/" + f.workspace + "/jobs/" + jobID + "/attempts/1/"
	if attempt != 1 {
		prefix = "workspaces/" + f.workspace + "/jobs/" + jobID + "/attempts/2/"
	}
	resultDigest := digest("result-" + jobID)
	completion := controlplane.Completion{
		ArtifactID:    testID(t),
		Kind:          operation.kind,
		ObjectKey:     prefix + operation.kind,
		ObjectVersion: resultDigest,
		Digest:        resultDigest,
		Size:          64,
		MediaType:     "application/json",
		SchemaName:    "result-envelope",
		SchemaVersion: 1,
	}
	if operation.companion != "" {
		companionDigest := digest("companion-" + jobID)
		completion.Companions = []controlplane.CompanionArtifact{{
			ArtifactID:    testID(t),
			Kind:          operation.companion,
			ObjectKey:     prefix + operation.companion,
			ObjectVersion: companionDigest,
			Digest:        companionDigest,
			Size:          128,
			MediaType:     operation.media,
		}}
	}
	if operation.name == "validate" {
		completion.BundleDigest = bundleDigest
		completion.LogicalDigest = digest("logical-" + jobID)
		completion.EventCount = 1
		completion.BundleFormat = 1
		completion.CassetteFormat = 1
		completion.EventSchema = 3
	}
	return completion
}

// fenceAndRetry expires the current attempt's lease and returns the claim the
// reconciler's scheduled retry hands to the next worker.
func (f *fixture) fenceAndRetry(t *testing.T, jobID string) controlplane.Claim {
	t.Helper()
	ctx := context.Background()
	if _, err := f.pool.Exec(ctx, "UPDATE job_attempts SET lease_expires_at=transaction_timestamp()-interval '1 second' WHERE job_id=$1", jobID); err != nil {
		t.Fatal(err)
	}
	if _, err := f.service.Reconcile(ctx, 100); err != nil {
		t.Fatal(err)
	}
	if _, err := f.pool.Exec(ctx, "UPDATE jobs SET retry_at=transaction_timestamp()-interval '1 second' WHERE id=$1", jobID); err != nil {
		t.Fatal(err)
	}
	if _, err := f.service.Reconcile(ctx, 100); err != nil {
		t.Fatal(err)
	}
	claim, err := f.service.Claim(ctx, f.worker, jobID)
	if err != nil {
		t.Fatal(err)
	}
	return claim
}

func TestEveryOperationRetriesFencesAndCommitsOneResult(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	for _, operation := range operations() {
		t.Run(operation.name, func(t *testing.T) {
			jobID, bundleDigest := f.job(t, operation.name, "retry-"+operation.name)
			first, err := f.service.Claim(ctx, f.worker, jobID)
			if err != nil {
				t.Fatal(err)
			}
			if err := f.service.UpdateProgress(ctx, jobID, 1, first.AttemptToken, controlplane.Progress{Sequence: 1, Stage: "analyzing", Message: "working"}); err != nil {
				t.Fatal(err)
			}
			second := f.fenceAndRetry(t, jobID)
			if second.Attempt != 2 {
				t.Fatalf("retry attempt=%d", second.Attempt)
			}
			if second.Operation != operation.name {
				t.Fatalf("claim operation=%q", second.Operation)
			}

			if _, err := f.service.Heartbeat(ctx, jobID, 1, first.AttemptToken); !errors.Is(err, controlplane.ErrLeaseLost) {
				t.Fatalf("stale heartbeat: %v", err)
			}
			if err := f.service.UpdateProgress(ctx, jobID, 1, first.AttemptToken, controlplane.Progress{Sequence: 2, Stage: "uploading", Message: "late"}); !errors.Is(err, controlplane.ErrLeaseLost) {
				t.Fatalf("stale progress: %v", err)
			}
			stale := f.completion(t, jobID, 1, operation, bundleDigest)
			if err := f.service.CompleteAttempt(ctx, jobID, 1, first.AttemptToken, stale); !errors.Is(err, controlplane.ErrLeaseLost) {
				t.Fatalf("stale completion: %v", err)
			}

			mismatched := f.completion(t, jobID, 2, operation, bundleDigest)
			mismatched.Kind = "worker_diagnostic"
			if err := f.service.CompleteAttempt(ctx, jobID, 2, second.AttemptToken, mismatched); err == nil {
				t.Fatal("a result of the wrong kind completed the job")
			}
			outside := f.completion(t, jobID, 2, operation, bundleDigest)
			outside.ObjectKey = "workspaces/" + f.workspace + "/jobs/" + jobID + "/attempts/1/" + operation.kind
			if err := f.service.CompleteAttempt(ctx, jobID, 2, second.AttemptToken, outside); err == nil {
				t.Fatal("a result outside the current attempt completed the job")
			}

			if err := f.service.CompleteAttempt(ctx, jobID, 2, second.AttemptToken, f.completion(t, jobID, 2, operation, bundleDigest)); err != nil {
				t.Fatal(err)
			}
			var kinds []string
			if err := f.pool.QueryRow(ctx, "SELECT array_agg(kind::text ORDER BY kind) FROM artifacts WHERE job_id=$1", jobID).Scan(&kinds); err != nil {
				t.Fatal(err)
			}
			expected := 1
			if operation.companion != "" {
				expected = 2
			}
			if len(kinds) != expected {
				t.Fatalf("registered artifacts=%v", kinds)
			}
			var state, resultKind string
			if err := f.pool.QueryRow(ctx, `SELECT j.state,a.kind::text FROM jobs j JOIN artifacts a ON a.id=j.result_artifact_id WHERE j.id=$1`, jobID).Scan(&state, &resultKind); err != nil {
				t.Fatal(err)
			}
			if state != "succeeded" || resultKind != operation.kind {
				t.Fatalf("job state=%q result kind=%q", state, resultKind)
			}
		})
	}
}

func TestEveryOperationResolvesCancellationAtTheDatabase(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	for _, operation := range operations() {
		t.Run(operation.name, func(t *testing.T) {
			jobID, bundleDigest := f.job(t, operation.name, "cancel-"+operation.name)
			claim, err := f.service.Claim(ctx, f.worker, jobID)
			if err != nil {
				t.Fatal(err)
			}
			if cancelled, err := f.service.Cancellation(ctx, jobID, 1, claim.AttemptToken); err != nil || cancelled {
				t.Fatalf("cancellation before any request=%v err=%v", cancelled, err)
			}
			if err := f.service.RequestCancellation(ctx, f.principal, jobID); err != nil {
				t.Fatal(err)
			}
			// Cancellation resolves in one transition, so the attempt it fenced
			// learns through lease loss rather than a later poll.
			if _, err := f.service.Cancellation(ctx, jobID, 1, claim.AttemptToken); !errors.Is(err, controlplane.ErrLeaseLost) {
				t.Fatalf("cancelled attempt: %v", err)
			}
			if err := f.service.CompleteAttempt(ctx, jobID, 1, claim.AttemptToken, f.completion(t, jobID, 1, operation, bundleDigest)); !errors.Is(err, controlplane.ErrLeaseLost) {
				t.Fatalf("completion after cancellation: %v", err)
			}
			var state string
			var artifacts int
			if err := f.pool.QueryRow(ctx, "SELECT j.state,(SELECT count(*) FROM artifacts WHERE job_id=j.id) FROM jobs j WHERE j.id=$1", jobID).Scan(&state, &artifacts); err != nil {
				t.Fatal(err)
			}
			if state != "cancelled" || artifacts != 0 {
				t.Fatalf("job state=%q artifacts=%d", state, artifacts)
			}
			if operation.name == "validate" {
				var runState string
				if err := f.pool.QueryRow(ctx, `SELECT r.state::text FROM runs r JOIN job_inputs i ON i.run_a_id=r.id JOIN jobs j ON j.input_id=i.id WHERE j.id=$1`, jobID).Scan(&runState); err != nil {
					t.Fatal(err)
				}
				if runState == "ready" {
					t.Fatal("a cancelled validation left the run usable")
				}
			}
		})
	}
}
