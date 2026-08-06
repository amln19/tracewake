package controlplane_test

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"sync"
	"testing"

	"github.com/amln19/locus/controlplane/internal/controlplane"
	"github.com/amln19/locus/controlplane/internal/store"
)

func testID(t *testing.T) string {
	t.Helper()
	var value [16]byte
	if _, err := rand.Read(value[:]); err != nil {
		t.Fatal(err)
	}
	value[6] = value[6]&0x0f | 0x40
	value[8] = value[8]&0x3f | 0x80
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x", value[:4], value[4:6], value[6:8], value[8:10], value[10:])
}
func digest(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

func TestPostgresLifecycle(t *testing.T) {
	databaseURL := os.Getenv("LOCUS_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("LOCUS_TEST_DATABASE_URL is not set")
	}
	ctx := context.Background()
	database, err := store.Open(ctx, databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	ring := controlplane.KeyRing{CurrentVersion: 1, Current: []byte("integration-test-pepper-material")}
	service, err := controlplane.New(database.Pool(), ring, ring)
	if err != nil {
		t.Fatal(err)
	}
	workspace, token, err := service.CreateWorkspace(ctx, "integration", []string{"runs:read", "runs:write", "jobs:read", "jobs:write"})
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
	runA, runB, runC := testID(t), testID(t), testID(t)
	for _, run := range []string{runA, runB, runC} {
		_, err = database.Pool().Exec(ctx, `INSERT INTO runs(id,workspace_id,state,declared_bundle_format,declared_bundle_digest,declared_bundle_size,bundle_object_key,bundle_object_version,validated_bundle_format,cassette_format_version,event_schema_version,logical_run_digest,event_count,ready_at) VALUES($1,$2,'ready',1,$3,1,$4,$5,1,1,3,$6,1,transaction_timestamp())`, run, workspace, digest("bundle-"+run), "workspaces/"+workspace+"/runs/"+run+"/bundle.tar", digest("version-"+run), digest("logical-"+run))
		if err != nil {
			t.Fatal(err)
		}
	}
	profile := "lexical-v1"
	request := controlplane.JobRequest{Operation: "diff", RunIDs: []string{runA, runB}, Profile: &profile}
	job, reused, err := service.CreateJob(ctx, principal, "retry-test", request)
	if err != nil || reused {
		t.Fatalf("create job: %v reused=%v", err, reused)
	}
	replayed, reused, err := service.CreateJob(ctx, principal, "retry-test", request)
	if err != nil || !reused || replayed.ID != job.ID {
		t.Fatalf("idempotent replay: %#v %v %v", replayed, reused, err)
	}
	changed := controlplane.JobRequest{Operation: "diff", RunIDs: []string{runB, runA}, Profile: &profile}
	if _, _, err := service.CreateJob(ctx, principal, "retry-test", changed); !errors.Is(err, controlplane.ErrIdempotencyConflict) {
		t.Fatalf("changed idempotency reuse: %v", err)
	}
	claim1, err := service.Claim(ctx, worker, job.ID)
	if err != nil {
		t.Fatal(err)
	}
	if claim1.Attempt != 1 {
		t.Fatalf("attempt=%d", claim1.Attempt)
	}
	if _, err := service.Claim(ctx, worker, job.ID); !errors.Is(err, controlplane.ErrConflict) {
		t.Fatalf("duplicate claim: %v", err)
	}
	if err := service.UpdateProgress(ctx, job.ID, 1, claim1.AttemptToken, controlplane.Progress{Sequence: 1, Stage: "analyzing", Message: "working"}); err != nil {
		t.Fatal(err)
	}
	if err := service.UpdateProgress(ctx, job.ID, 1, claim1.AttemptToken, controlplane.Progress{Sequence: 1, Stage: "analyzing", Message: "working"}); err != nil {
		t.Fatal(err)
	}
	if _, err = database.Pool().Exec(ctx, "UPDATE job_attempts SET lease_expires_at=transaction_timestamp()-interval '1 second' WHERE job_id=$1 AND attempt_number=1", job.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := service.Reconcile(ctx, 100); err != nil {
		t.Fatal(err)
	}
	if _, err = database.Pool().Exec(ctx, "UPDATE jobs SET retry_at=transaction_timestamp()-interval '1 second' WHERE id=$1", job.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := service.Reconcile(ctx, 100); err != nil {
		t.Fatal(err)
	}
	claim2, err := service.Claim(ctx, worker, job.ID)
	if err != nil {
		t.Fatal(err)
	}
	if claim2.Attempt != 2 {
		t.Fatalf("retry attempt=%d", claim2.Attempt)
	}
	if _, err := service.Heartbeat(ctx, job.ID, 1, claim1.AttemptToken); !errors.Is(err, controlplane.ErrLeaseLost) {
		t.Fatalf("stale heartbeat: %v", err)
	}
	if err := service.RequestCancellation(ctx, principal, job.ID); err != nil {
		t.Fatal(err)
	}
	if err := service.CompleteAttempt(ctx, job.ID, 2, claim2.AttemptToken, controlplane.Completion{}); !errors.Is(err, controlplane.ErrLeaseLost) {
		t.Fatalf("late completion: %v", err)
	}
	view, err := service.GetJob(ctx, principal, job.ID)
	if err != nil || view.State != "cancelled" {
		t.Fatalf("cancelled view: %#v %v", view, err)
	}
	job2, _, err := service.CreateJob(ctx, principal, "success-test", changed)
	if err != nil {
		t.Fatal(err)
	}
	claim, err := service.Claim(ctx, worker, job2.ID)
	if err != nil {
		t.Fatal(err)
	}
	artifactID := testID(t)
	resultDigest := digest("result")
	completion := controlplane.Completion{ArtifactID: artifactID, Kind: "diff_json", ObjectKey: "workspaces/" + workspace + "/jobs/" + job2.ID + "/attempts/1/diff_json", ObjectVersion: resultDigest, Digest: resultDigest, Size: 10, MediaType: "application/json", SchemaName: "result-envelope", SchemaVersion: 1}
	if err := service.CompleteAttempt(ctx, job2.ID, 1, claim.AttemptToken, completion); err != nil {
		t.Fatal(err)
	}
	if err := service.RequestCancellation(ctx, principal, job2.ID); err != nil {
		t.Fatal(err)
	}
	view, err = service.GetJob(ctx, principal, job2.ID)
	if err != nil || view.State != "succeeded" {
		t.Fatalf("immutable success: %#v %v", view, err)
	}
	strandedRequest := controlplane.JobRequest{Operation: "diff", RunIDs: []string{runA, runC}, Profile: &profile}
	stranded, _, err := service.CreateJob(ctx, principal, "stranded", strandedRequest)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := database.Pool().Exec(ctx, "UPDATE outbox SET published_at=transaction_timestamp() WHERE aggregate_id=$1", stranded.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := service.Reconcile(ctx, 100); err != nil {
		t.Fatal(err)
	}
	var unpublished int
	if err := database.Pool().QueryRow(ctx, "SELECT count(*) FROM outbox WHERE aggregate_id=$1 AND published_at IS NULL", stranded.ID).Scan(&unpublished); err != nil || unpublished != 1 {
		t.Fatalf("stranded outbox count=%d err=%v", unpublished, err)
	}
	if err := service.RequestCancellation(ctx, principal, stranded.ID); err != nil {
		t.Fatal(err)
	}
	raceRequest := controlplane.JobRequest{Operation: "diff", RunIDs: []string{runB, runC}, Profile: &profile}
	raceJob, _, err := service.CreateJob(ctx, principal, "race", raceRequest)
	if err != nil {
		t.Fatal(err)
	}
	raceClaim, err := service.Claim(ctx, worker, raceJob.ID)
	if err != nil {
		t.Fatal(err)
	}
	raceDigest := digest("race-result")
	raceCompletion := controlplane.Completion{ArtifactID: testID(t), Kind: "diff_json", ObjectKey: "workspaces/" + workspace + "/jobs/" + raceJob.ID + "/attempts/1/diff_json", ObjectVersion: raceDigest, Digest: raceDigest, Size: 10, MediaType: "application/json", SchemaName: "result-envelope", SchemaVersion: 1}
	start := make(chan struct{})
	var wait sync.WaitGroup
	wait.Add(2)
	var cancelErr, completeErr error
	go func() {
		defer wait.Done()
		<-start
		cancelErr = service.RequestCancellation(ctx, principal, raceJob.ID)
	}()
	go func() {
		defer wait.Done()
		<-start
		completeErr = service.CompleteAttempt(ctx, raceJob.ID, 1, raceClaim.AttemptToken, raceCompletion)
	}()
	close(start)
	wait.Wait()
	if cancelErr != nil {
		t.Fatal(cancelErr)
	}
	if completeErr != nil && !errors.Is(completeErr, controlplane.ErrLeaseLost) {
		t.Fatal(completeErr)
	}
	raceView, err := service.GetJob(ctx, principal, raceJob.ID)
	if err != nil || (raceView.State != "cancelled" && raceView.State != "succeeded") {
		t.Fatalf("race result: %#v %v", raceView, err)
	}
	other, _, err := service.CreateWorkspace(ctx, "other", []string{"runs:read"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.GetRun(ctx, controlplane.Principal{WorkspaceID: other}, runA); !errors.Is(err, controlplane.ErrNotFound) {
		t.Fatalf("tenant isolation: %v", err)
	}
}
