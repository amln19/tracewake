package controlplane

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/amln19/tracewake/controlplane/internal/artifacts"
	"github.com/amln19/tracewake/controlplane/internal/telemetry"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
)

type Notification struct {
	ID      int64
	Payload json.RawMessage
}

// notificationTimeout is how long a queued job may wait after its last
// notification before the reconciler treats it as stranded.
const notificationTimeout = "60 seconds"

func (s *Service) RetainedObjects(ctx context.Context) (map[artifacts.Identity]bool, error) {
	rows, err := s.pool.Query(ctx, `SELECT bundle_object_key,bundle_object_version FROM runs WHERE state<>'deleted' AND retention_expires_at>transaction_timestamp() AND bundle_object_version IS NOT NULL UNION SELECT object_key,object_version FROM artifacts WHERE authoritative AND retention_expires_at>transaction_timestamp()`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[artifacts.Identity]bool{}
	for rows.Next() {
		var identity artifacts.Identity
		if err := rows.Scan(&identity.Key, &identity.Version); err != nil {
			return nil, err
		}
		result[identity] = true
	}
	return result, rows.Err()
}

func (s *Service) NextNotification(ctx context.Context) (Notification, error) {
	var notification Notification
	err := s.pool.QueryRow(ctx, `SELECT id,payload FROM outbox WHERE topic='job.created' AND available_at<=transaction_timestamp() AND published_at IS NULL ORDER BY id LIMIT 1`).Scan(&notification.ID, &notification.Payload)
	return notification, err
}

// Notifier delivers an outbox payload to the hosted queue.
type Notifier interface {
	Publish(ctx context.Context, payload []byte) error
}

// PublishOutbox sends unpublished notifications and records publication in the
// same transaction that holds the rows. Publication happens before that commit,
// so a crash in the window redelivers a duplicate rather than stranding a job.
func (s *Service) PublishOutbox(ctx context.Context, notifier Notifier, limit int) (int, error) {
	if limit < 1 || limit > 100 {
		limit = 100
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	rows, err := tx.Query(ctx, `SELECT id,payload FROM outbox
        WHERE published_at IS NULL AND available_at<=transaction_timestamp()
        ORDER BY id FOR UPDATE SKIP LOCKED LIMIT $1`, limit)
	if err != nil {
		return 0, fmt.Errorf("claim outbox rows: %w", err)
	}
	var pending []Notification
	for rows.Next() {
		var item Notification
		if err := rows.Scan(&item.ID, &item.Payload); err != nil {
			rows.Close()
			return 0, err
		}
		pending = append(pending, item)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return 0, err
	}
	published := 0
	for _, item := range pending {
		publishCtx, span := telemetry.Span(telemetry.Continue(ctx, payloadTraceparent(item.Payload)), "outbox.publish", trace.SpanKindProducer)
		err := notifier.Publish(publishCtx, item.Payload)
		span.End()
		if err != nil {
			return published, err
		}
		if _, err := tx.Exec(ctx, `UPDATE outbox SET published_at=transaction_timestamp(),publish_attempts=publish_attempts+1 WHERE id=$1`, item.ID); err != nil {
			return published, err
		}
		published++
	}
	if err := tx.Commit(ctx); err != nil {
		return 0, fmt.Errorf("record outbox publication: %w", err)
	}
	s.metrics.OutboxPublished(ctx, "job.created", published)
	return published, nil
}

func payloadTraceparent(payload json.RawMessage) string {
	var message struct {
		Traceparent string `json:"traceparent"`
	}
	if json.Unmarshal(payload, &message) != nil {
		return ""
	}
	return message.Traceparent
}

func (s *Service) AcknowledgeNotification(ctx context.Context, id int64) error {
	_, err := s.pool.Exec(ctx, `UPDATE outbox SET published_at=COALESCE(published_at,transaction_timestamp()),publish_attempts=publish_attempts+1 WHERE id=$1`, id)
	return err
}

func (s *Service) Reconcile(ctx context.Context, limit int) (repaired int, err error) {
	if limit < 1 || limit > 100 {
		limit = 100
	}
	ctx, span := telemetry.Span(ctx, "reconcile", trace.SpanKindInternal)
	// The backlog is a property of the database, not of whoever publishes from
	// it, so it is measured on the pass that runs whether or not a queue exists.
	var pendingSeconds *float64
	if scanErr := s.pool.QueryRow(ctx, `SELECT EXTRACT(EPOCH FROM (transaction_timestamp()-min(created_at))) FROM outbox WHERE published_at IS NULL`).Scan(&pendingSeconds); scanErr == nil {
		age := 0.0
		if pendingSeconds != nil {
			age = *pendingSeconds
		}
		s.metrics.OutboxPendingAge(ctx, time.Duration(age*float64(time.Second)))
	}
	defer func() {
		span.SetAttributes(attribute.Int("tracewake.repairs", repaired), attribute.Bool(telemetry.IdleAttribute, repaired == 0))
		if err != nil {
			s.metrics.ReconcileFailed(ctx)
		}
		span.End()
	}()
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	rows, err := tx.Query(ctx, `SELECT j.id,j.current_attempt_number,i.operation,EXTRACT(EPOCH FROM (transaction_timestamp()-j.created_at)) FROM jobs j JOIN job_inputs i ON i.id=j.input_id JOIN job_attempts a ON a.job_id=j.id AND a.attempt_number=j.current_attempt_number
        WHERE j.state='running' AND a.state='running' AND a.lease_expires_at<=transaction_timestamp() ORDER BY a.lease_expires_at FOR UPDATE OF j,a SKIP LOCKED LIMIT $1`, limit)
	if err != nil {
		return 0, fmt.Errorf("find expired attempts: %w", err)
	}
	type expired struct {
		id        string
		attempt   int
		operation string
		age       float64
	}
	var items []expired
	for rows.Next() {
		var item expired
		if err := rows.Scan(&item.id, &item.attempt, &item.operation, &item.age); err != nil {
			rows.Close()
			return 0, err
		}
		items = append(items, item)
	}
	rows.Close()
	var fencedItems, exhaustedItems []expired
	for _, item := range items {
		if item.attempt < 3 {
			fencedItems = append(fencedItems, item)
			delay := "5 seconds"
			if item.attempt == 2 {
				delay = "30 seconds"
			}
			if _, err = tx.Exec(ctx, `UPDATE job_attempts SET state='fenced',finished_at=transaction_timestamp(),failure_code='lease_lost',failure_message='worker lease expired' WHERE job_id=$1 AND attempt_number=$2 AND state='running'`, item.id, item.attempt); err != nil {
				return 0, err
			}
			if _, err = tx.Exec(ctx, `UPDATE jobs SET state='retry_wait',current_attempt_number=NULL,retry_at=transaction_timestamp()+$2::interval,updated_at=transaction_timestamp(),row_version=row_version+1 WHERE id=$1 AND state='running'`, item.id, delay); err != nil {
				return 0, err
			}
		} else {
			exhaustedItems = append(exhaustedItems, item)
			if _, err = tx.Exec(ctx, `UPDATE job_attempts SET state='failed',finished_at=transaction_timestamp(),failure_code='retry_exhausted',failure_message='worker lease expired' WHERE job_id=$1 AND attempt_number=$2`, item.id, item.attempt); err != nil {
				return 0, err
			}
			if _, err = tx.Exec(ctx, `UPDATE jobs SET state='failed',terminal_at=transaction_timestamp(),failure_code='retry_exhausted',failure_message='worker lease expired',updated_at=transaction_timestamp(),row_version=row_version+1 WHERE id=$1`, item.id); err != nil {
				return 0, err
			}
			if _, err = tx.Exec(ctx, `UPDATE runs r SET state='invalid',failure_code='retry_exhausted',failure_message='worker lease expired',row_version=row_version+1 FROM job_inputs i WHERE i.id=(SELECT input_id FROM jobs WHERE id=$1) AND i.operation='validate' AND r.id=i.run_a_id AND r.state='validating'`, item.id); err != nil {
				return 0, err
			}
		}
	}
	parent := telemetry.Traceparent(ctx)
	command, err := tx.Exec(ctx, `WITH due AS (SELECT id FROM jobs WHERE state='retry_wait' AND retry_at<=transaction_timestamp() AND cancel_requested_at IS NULL FOR UPDATE SKIP LOCKED LIMIT $1), updated AS (UPDATE jobs j SET state='queued',retry_at=NULL,updated_at=transaction_timestamp(),row_version=row_version+1 FROM due WHERE j.id=due.id RETURNING j.id,j.row_version,(SELECT operation FROM job_inputs WHERE id=j.input_id) operation) INSERT INTO outbox(aggregate_type,aggregate_id,aggregate_version,topic,payload) SELECT 'job',id,row_version,'job.created',jsonb_strip_nulls(jsonb_build_object('protocol_version',1,'job_id',id,'job_version',row_version,'operation',operation,'traceparent',NULLIF($2,''))) FROM updated`, limit, parent)
	if err != nil {
		return 0, err
	}
	// A queued job whose notification was published but never claimed is only
	// stranded once delivery has had time to happen; republishing sooner would
	// storm the queue with duplicates of healthy work.
	stranded, err := tx.Exec(ctx, `WITH candidates AS (SELECT j.id FROM jobs j WHERE j.state='queued' AND j.updated_at<transaction_timestamp()-$2::interval AND NOT EXISTS(SELECT 1 FROM outbox o WHERE o.aggregate_id=j.id AND o.topic='job.created' AND (o.published_at IS NULL OR o.created_at>transaction_timestamp()-$2::interval)) FOR UPDATE SKIP LOCKED LIMIT $1), updated AS (UPDATE jobs j SET row_version=row_version+1,updated_at=transaction_timestamp() FROM candidates WHERE j.id=candidates.id RETURNING j.id,j.row_version,(SELECT operation FROM job_inputs WHERE id=j.input_id) operation) INSERT INTO outbox(aggregate_type,aggregate_id,aggregate_version,topic,payload) SELECT 'job',id,row_version,'job.created',jsonb_strip_nulls(jsonb_build_object('protocol_version',1,'job_id',id,'job_version',row_version,'operation',operation,'traceparent',NULLIF($3,''))) FROM updated`, limit, notificationTimeout, parent)
	if err != nil {
		return 0, err
	}
	if err = tx.Commit(ctx); err != nil {
		return 0, err
	}
	s.metrics.ReconcileAction(ctx, "lease_fenced", len(fencedItems))
	s.metrics.ReconcileAction(ctx, "retry_exhausted", len(exhaustedItems))
	s.metrics.ReconcileAction(ctx, "retry_scheduled", int(command.RowsAffected()))
	s.metrics.ReconcileAction(ctx, "republished", int(stranded.RowsAffected()))
	for range fencedItems {
		s.metrics.AttemptFenced(ctx, "lease_expired")
	}
	for _, item := range exhaustedItems {
		s.metrics.AttemptFenced(ctx, "retry_exhausted")
		s.metrics.JobTerminal(ctx, item.operation, "failed", time.Duration(item.age*float64(time.Second)))
	}
	return len(items) + int(command.RowsAffected()) + int(stranded.RowsAffected()), nil
}
