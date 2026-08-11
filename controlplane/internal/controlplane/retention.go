package controlplane

import (
	"context"
	"fmt"

	"github.com/amln19/tracewake/controlplane/internal/telemetry"
	"go.opentelemetry.io/otel/trace"
)

// publishedNotificationRetention bounds the outbox. Published rows are
// operational history, not lifecycle authority, and the reconciler only ever
// looks at recent ones.
const publishedNotificationRetention = "7 days"

// Retention reports what one enforcement pass removed.
type Retention struct {
	RunsExpired            int64 `json:"runs_expired"`
	OrphanArtifactsRemoved int64 `json:"orphan_artifacts_removed"`
	IdempotencyRecordsGone int64 `json:"idempotency_records_removed"`
	AuditRecordsGone       int64 `json:"audit_records_removed"`
	PublishedNotifications int64 `json:"published_notifications_removed"`
}

// EnforceRetention applies the deployment's retention windows to hosted state.
//
// Rows that a successful job depends on are never removed here: an artifact
// row is the record of what a job committed, so it outlives the bytes. Once
// its retention has passed the object stops being retained and the API stops
// offering it, which is what makes the data gone.
func (s *Service) EnforceRetention(ctx context.Context) (Retention, error) {
	ctx, span := telemetry.Span(ctx, "retention.enforce", trace.SpanKindInternal)
	defer span.End()
	var applied Retention
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return applied, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	for _, step := range []struct {
		statement string
		count     *int64
	}{
		{`UPDATE runs SET state='deleted',row_version=row_version+1
          WHERE state<>'deleted' AND retention_expires_at<=transaction_timestamp()`, &applied.RunsExpired},
		{`DELETE FROM artifacts WHERE NOT authoritative AND retention_expires_at<=transaction_timestamp()`, &applied.OrphanArtifactsRemoved},
		{`DELETE FROM idempotency_records WHERE expires_at<=transaction_timestamp()`, &applied.IdempotencyRecordsGone},
		{`DELETE FROM audit_records WHERE retention_expires_at<=transaction_timestamp()`, &applied.AuditRecordsGone},
		{`DELETE FROM outbox WHERE published_at IS NOT NULL
          AND published_at<transaction_timestamp()-'` + publishedNotificationRetention + `'::interval`, &applied.PublishedNotifications},
	} {
		command, err := tx.Exec(ctx, step.statement)
		if err != nil {
			return applied, fmt.Errorf("enforce retention: %w", err)
		}
		*step.count = command.RowsAffected()
	}
	if _, err := tx.Exec(ctx, `UPDATE job_attempts a SET state='cancelled',finished_at=transaction_timestamp()
		FROM jobs j JOIN job_inputs i ON i.id=j.input_id
		WHERE a.job_id=j.id AND a.state='running' AND EXISTS (
			SELECT 1 FROM runs r WHERE r.id IN(i.run_a_id,i.run_b_id) AND r.state='deleted' AND r.retention_expires_at<=transaction_timestamp()
		)`); err != nil {
		return applied, fmt.Errorf("fence work whose inputs expired: %w", err)
	}
	if _, err := tx.Exec(ctx, `WITH cancelled AS (
		UPDATE jobs j SET state='cancelled',cancel_requested_at=COALESCE(cancel_requested_at,transaction_timestamp()),terminal_at=transaction_timestamp(),retry_at=NULL,updated_at=transaction_timestamp(),row_version=row_version+1
		FROM job_inputs i WHERE j.input_id=i.id AND j.state IN('queued','running','retry_wait') AND EXISTS (
			SELECT 1 FROM runs r WHERE r.id IN(i.run_a_id,i.run_b_id) AND r.state='deleted' AND r.retention_expires_at<=transaction_timestamp()
		) RETURNING j.id,j.workspace_id
	) INSERT INTO audit_records(workspace_id,aggregate_type,aggregate_id,event_type,actor_type,payload)
		SELECT workspace_id,'job',id,'job.cancelled','retention','{}'::jsonb FROM cancelled`); err != nil {
		return applied, fmt.Errorf("cancel work whose inputs expired: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return applied, fmt.Errorf("commit retention: %w", err)
	}
	return applied, nil
}

// DeleteRun makes a run and everything derived from it inaccessible now, and
// lets object cleanup remove the bytes. Deletion is a tenant request, so it
// takes effect immediately in the API rather than waiting for a sweep.
func (s *Service) DeleteRun(ctx context.Context, principal Principal, runID string) error {
	ctx, span := telemetry.Span(ctx, "run.delete", trace.SpanKindInternal)
	defer span.End()
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	command, err := tx.Exec(ctx, `UPDATE runs SET state='deleted',retention_expires_at=transaction_timestamp(),row_version=row_version+1
        WHERE id=$1 AND workspace_id=$2 AND state<>'deleted'`, runID, principal.WorkspaceID)
	if err != nil {
		return fmt.Errorf("delete run: %w", err)
	}
	if command.RowsAffected() != 1 {
		return ErrNotFound
	}
	if _, err := tx.Exec(ctx, `UPDATE job_attempts a SET state='cancelled',finished_at=transaction_timestamp()
		FROM jobs j JOIN job_inputs i ON i.id=j.input_id
		WHERE a.job_id=j.id AND a.state='running' AND j.workspace_id=$2 AND $1 IN(i.run_a_id,i.run_b_id)`, runID, principal.WorkspaceID); err != nil {
		return fmt.Errorf("fence work derived from deleted run: %w", err)
	}
	if _, err := tx.Exec(ctx, `WITH cancelled AS (
		UPDATE jobs j SET state='cancelled',cancel_requested_at=COALESCE(cancel_requested_at,transaction_timestamp()),terminal_at=transaction_timestamp(),retry_at=NULL,updated_at=transaction_timestamp(),row_version=row_version+1
		FROM job_inputs i WHERE j.input_id=i.id AND j.workspace_id=$2 AND $1 IN(i.run_a_id,i.run_b_id) AND j.state IN('queued','running','retry_wait')
		RETURNING j.id,j.workspace_id
	) INSERT INTO audit_records(workspace_id,aggregate_type,aggregate_id,event_type,actor_type,payload)
		SELECT workspace_id,'job',id,'job.cancelled','tenant','{}'::jsonb FROM cancelled`, runID, principal.WorkspaceID); err != nil {
		return fmt.Errorf("cancel work derived from deleted run: %w", err)
	}
	if _, err := tx.Exec(ctx, `UPDATE artifacts a SET retention_expires_at=transaction_timestamp()
        FROM jobs j JOIN job_inputs i ON i.id=j.input_id
        WHERE a.job_id=j.id AND a.workspace_id=$2 AND $1 IN (i.run_a_id,i.run_b_id)`, runID, principal.WorkspaceID); err != nil {
		return fmt.Errorf("expire derived artifacts: %w", err)
	}
	if _, err := tx.Exec(ctx, `INSERT INTO audit_records(workspace_id,aggregate_type,aggregate_id,event_type,actor_type,payload)
        VALUES($1,'run',$2,'run.deleted','tenant','{}')`, principal.WorkspaceID, runID); err != nil {
		return fmt.Errorf("audit run deletion: %w", err)
	}
	return tx.Commit(ctx)
}
