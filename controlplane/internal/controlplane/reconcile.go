package controlplane

import (
	"context"
	"encoding/json"
	"fmt"
)

type Notification struct {
	ID      int64
	Payload json.RawMessage
}

func (s *Service) NextNotification(ctx context.Context) (Notification, error) {
	var notification Notification
	err := s.pool.QueryRow(ctx, `SELECT id,payload FROM outbox WHERE topic='job.created' AND available_at<=transaction_timestamp() AND published_at IS NULL ORDER BY id LIMIT 1`).Scan(&notification.ID, &notification.Payload)
	return notification, err
}

func (s *Service) AcknowledgeNotification(ctx context.Context, id int64) error {
	_, err := s.pool.Exec(ctx, `UPDATE outbox SET published_at=COALESCE(published_at,transaction_timestamp()),publish_attempts=publish_attempts+1 WHERE id=$1`, id)
	return err
}

func (s *Service) Reconcile(ctx context.Context, limit int) (int, error) {
	if limit < 1 || limit > 100 {
		limit = 100
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	rows, err := tx.Query(ctx, `SELECT j.id,j.current_attempt_number FROM jobs j JOIN job_attempts a ON a.job_id=j.id AND a.attempt_number=j.current_attempt_number
        WHERE j.state='running' AND a.state='running' AND a.lease_expires_at<=transaction_timestamp() ORDER BY a.lease_expires_at FOR UPDATE OF j,a SKIP LOCKED LIMIT $1`, limit)
	if err != nil {
		return 0, fmt.Errorf("find expired attempts: %w", err)
	}
	type expired struct {
		id      string
		attempt int
	}
	var items []expired
	for rows.Next() {
		var item expired
		if err := rows.Scan(&item.id, &item.attempt); err != nil {
			rows.Close()
			return 0, err
		}
		items = append(items, item)
	}
	rows.Close()
	for _, item := range items {
		if item.attempt < 3 {
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
			if _, err = tx.Exec(ctx, `UPDATE job_attempts SET state='failed',finished_at=transaction_timestamp(),failure_code='retry_exhausted',failure_message='worker lease expired' WHERE job_id=$1 AND attempt_number=$2`, item.id, item.attempt); err != nil {
				return 0, err
			}
			if _, err = tx.Exec(ctx, `UPDATE jobs SET state='failed',terminal_at=transaction_timestamp(),failure_code='retry_exhausted',failure_message='worker lease expired',updated_at=transaction_timestamp(),row_version=row_version+1 WHERE id=$1`, item.id); err != nil {
				return 0, err
			}
		}
	}
	command, err := tx.Exec(ctx, `WITH due AS (SELECT id FROM jobs WHERE state='retry_wait' AND retry_at<=transaction_timestamp() AND cancel_requested_at IS NULL FOR UPDATE SKIP LOCKED LIMIT $1), updated AS (UPDATE jobs j SET state='queued',retry_at=NULL,updated_at=transaction_timestamp(),row_version=row_version+1 FROM due WHERE j.id=due.id RETURNING j.id,j.row_version,(SELECT operation FROM job_inputs WHERE id=j.input_id) operation) INSERT INTO outbox(aggregate_type,aggregate_id,aggregate_version,topic,payload) SELECT 'job',id,row_version,'job.created',jsonb_build_object('protocol_version',1,'job_id',id,'job_version',row_version,'operation',operation) FROM updated`, limit)
	if err != nil {
		return 0, err
	}
	if err = tx.Commit(ctx); err != nil {
		return 0, err
	}
	return len(items) + int(command.RowsAffected()), nil
}
