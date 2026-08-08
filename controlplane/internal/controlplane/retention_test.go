package controlplane_test

import (
	"context"
	"errors"
	"testing"

	"github.com/amln19/tracewake/controlplane/internal/controlplane"
)

// succeed drives one job to a committed result the way a worker would.
func (f *fixture) succeed(t *testing.T, jobID string, operation operationCase, bundleDigest string) {
	t.Helper()
	ctx := context.Background()
	claim, err := f.service.Claim(ctx, f.worker, jobID)
	if err != nil {
		t.Fatal(err)
	}
	if err := f.service.CompleteAttempt(ctx, jobID, claim.Attempt, claim.AttemptToken, f.completion(t, jobID, claim.Attempt, operation, bundleDigest)); err != nil {
		t.Fatal(err)
	}
}

func TestDeletionHidesARunImmediately(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	run := f.readyRun(t)
	if _, err := f.service.GetRun(ctx, f.principal, run); err != nil {
		t.Fatal(err)
	}
	if err := f.service.DeleteRun(ctx, f.principal, run); err != nil {
		t.Fatal(err)
	}
	if _, err := f.service.GetRun(ctx, f.principal, run); !errors.Is(err, controlplane.ErrNotFound) {
		t.Fatalf("a deleted run is still readable: %v", err)
	}
	runs, err := f.service.ListRuns(ctx, f.principal, 100)
	if err != nil {
		t.Fatal(err)
	}
	for _, listed := range runs {
		if listed.ID == run {
			t.Fatal("a deleted run is still listed")
		}
	}
	keys, err := f.service.RetainedObjectKeys(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var key string
	if err := f.pool.QueryRow(ctx, "SELECT bundle_object_key FROM runs WHERE id=$1", run).Scan(&key); err != nil {
		t.Fatal(err)
	}
	if keys[key] {
		t.Fatal("a deleted run's bundle is still retained")
	}
	if err := f.service.DeleteRun(ctx, f.principal, run); !errors.Is(err, controlplane.ErrNotFound) {
		t.Fatalf("deleting twice did not report the run as gone: %v", err)
	}
}

func TestDeletionRemovesWhatWasDerivedFromTheRun(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	jobID, _ := f.job(t, "otlp", "deletion-"+testID(t))
	f.succeed(t, jobID, operationCase{name: "otlp", kind: "otlp_result_json", companion: "otlp_json", media: "application/json"}, "")
	job, err := f.service.GetJob(ctx, f.principal, jobID)
	if err != nil {
		t.Fatal(err)
	}
	if len(job.Artifacts) == 0 {
		t.Fatal("the job committed no artifacts to delete")
	}
	artifact := job.Artifacts[0].ID
	var run string
	if err := f.pool.QueryRow(ctx, `SELECT i.run_a_id FROM jobs j JOIN job_inputs i ON i.id=j.input_id WHERE j.id=$1`, jobID).Scan(&run); err != nil {
		t.Fatal(err)
	}
	if err := f.service.DeleteRun(ctx, f.principal, run); err != nil {
		t.Fatal(err)
	}
	if _, err := f.service.GetArtifact(ctx, f.principal, artifact); !errors.Is(err, controlplane.ErrNotFound) {
		t.Fatalf("an artifact derived from a deleted run is still downloadable: %v", err)
	}
	after, err := f.service.GetJob(ctx, f.principal, jobID)
	if err != nil {
		t.Fatal(err)
	}
	if len(after.Artifacts) != 0 {
		t.Fatalf("the job still offers %d artifacts of a deleted run", len(after.Artifacts))
	}
	// The job itself stays: it is the record that the analysis happened.
	if after.State != "succeeded" {
		t.Fatalf("deleting a run rewrote terminal job state to %q", after.State)
	}
}

func TestRetentionRemovesOnlyExpiredRows(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	live := f.readyRun(t)
	expired := f.readyRun(t)
	if _, err := f.pool.Exec(ctx, "UPDATE runs SET retention_expires_at=transaction_timestamp()-interval '1 day' WHERE id=$1", expired); err != nil {
		t.Fatal(err)
	}
	if _, err := f.pool.Exec(ctx, `INSERT INTO idempotency_records(workspace_id,operation,idempotency_key,request_digest,response_kind,response_id,created_at,expires_at)
        VALUES($1,'create_job',$2,$3,'job',$4,transaction_timestamp()-interval '2 days',transaction_timestamp()-interval '1 day')`,
		f.workspace, "expired-"+testID(t), digest("request"), testID(t)); err != nil {
		t.Fatal(err)
	}
	if _, err := f.pool.Exec(ctx, `INSERT INTO audit_records(workspace_id,aggregate_type,aggregate_id,event_type,actor_type,retention_expires_at)
        VALUES($1,'job',$2,'job.created','tenant',transaction_timestamp()-interval '1 day')`, f.workspace, testID(t)); err != nil {
		t.Fatal(err)
	}
	applied, err := f.service.EnforceRetention(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if applied.RunsExpired < 1 || applied.IdempotencyRecordsGone < 1 || applied.AuditRecordsGone < 1 {
		t.Fatalf("retention removed nothing it should have: %+v", applied)
	}
	if _, err := f.service.GetRun(ctx, f.principal, expired); !errors.Is(err, controlplane.ErrNotFound) {
		t.Fatalf("an expired run is still readable: %v", err)
	}
	if _, err := f.service.GetRun(ctx, f.principal, live); err != nil {
		t.Fatalf("retention removed a run inside its window: %v", err)
	}
}

func TestRetentionKeepsWhatASuccessfulJobRecorded(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	jobID, _ := f.job(t, "pprof", "retention-"+testID(t))
	f.succeed(t, jobID, operationCase{name: "pprof", kind: "pprof_result_json", companion: "pprof", media: "application/octet-stream"}, "")
	if _, err := f.pool.Exec(ctx, "UPDATE artifacts SET retention_expires_at=transaction_timestamp()-interval '1 day' WHERE job_id=$1", jobID); err != nil {
		t.Fatal(err)
	}
	if _, err := f.service.EnforceRetention(ctx); err != nil {
		t.Fatal(err)
	}
	var remaining int
	if err := f.pool.QueryRow(ctx, "SELECT count(*) FROM artifacts WHERE job_id=$1", jobID).Scan(&remaining); err != nil {
		t.Fatal(err)
	}
	if remaining != 2 {
		t.Fatalf("retention deleted %d of the rows a successful job depends on", 2-remaining)
	}
	job, err := f.service.GetJob(ctx, f.principal, jobID)
	if err != nil {
		t.Fatal(err)
	}
	if job.State != "succeeded" || len(job.Artifacts) != 0 {
		t.Fatalf("an expired job should stay successful with nothing to download: %s, %d artifacts", job.State, len(job.Artifacts))
	}
	keys, err := f.service.RetainedObjectKeys(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var key string
	if err := f.pool.QueryRow(ctx, "SELECT object_key FROM artifacts WHERE job_id=$1 AND authoritative", jobID).Scan(&key); err != nil {
		t.Fatal(err)
	}
	if keys[key] {
		t.Fatal("an expired artifact's object is still retained")
	}
}

func TestRetentionBoundsPublishedNotifications(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	if _, err := f.pool.Exec(ctx, `INSERT INTO outbox(aggregate_type,aggregate_id,aggregate_version,topic,payload,published_at,created_at)
        VALUES('job',$1,1,'job.created','{}'::jsonb,transaction_timestamp()-interval '30 days',transaction_timestamp()-interval '30 days')`, testID(t)); err != nil {
		t.Fatal(err)
	}
	recent := testID(t)
	if _, err := f.pool.Exec(ctx, `INSERT INTO outbox(aggregate_type,aggregate_id,aggregate_version,topic,payload,published_at)
        VALUES('job',$1,1,'job.created','{}'::jsonb,transaction_timestamp())`, recent); err != nil {
		t.Fatal(err)
	}
	applied, err := f.service.EnforceRetention(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if applied.PublishedNotifications < 1 {
		t.Fatal("an old published notification was kept")
	}
	var kept int
	if err := f.pool.QueryRow(ctx, "SELECT count(*) FROM outbox WHERE aggregate_id=$1", recent).Scan(&kept); err != nil {
		t.Fatal(err)
	}
	if kept != 1 {
		t.Fatal("a recent published notification was removed")
	}
}
