package controlplane

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

type Progress struct {
	ProtocolVersion int    `json:"protocol_version"`
	AttemptNumber   int    `json:"attempt_number"`
	Sequence        int64  `json:"sequence"`
	Stage           string `json:"stage"`
	Message         string `json:"message"`
}
type Completion struct {
	ArtifactID     string              `json:"artifact_id"`
	Kind           string              `json:"kind"`
	ObjectKey      string              `json:"object_key"`
	ObjectVersion  string              `json:"object_version"`
	Digest         string              `json:"digest"`
	MediaType      string              `json:"media_type"`
	SchemaName     string              `json:"schema_name"`
	Size           int64               `json:"size"`
	SchemaVersion  int                 `json:"schema_version"`
	LogicalDigest  string              `json:"logical_run_digest"`
	BundleDigest   string              `json:"bundle_digest"`
	EventCount     int                 `json:"event_count"`
	BundleFormat   int                 `json:"bundle_format_version"`
	CassetteFormat int                 `json:"cassette_format_version"`
	EventSchema    int                 `json:"event_schema_version"`
	Companions     []CompanionArtifact `json:"companions"`
}

type CompanionArtifact struct {
	ArtifactID    string  `json:"artifact_id"`
	Kind          string  `json:"kind"`
	ObjectKey     string  `json:"object_key"`
	ObjectVersion string  `json:"object_version"`
	Digest        string  `json:"digest"`
	Size          int64   `json:"size"`
	MediaType     string  `json:"media_type"`
	SchemaName    *string `json:"schema_name"`
	SchemaVersion *int    `json:"schema_version"`
}

func (s *Service) Cancellation(ctx context.Context, jobID string, attempt int, token string) (bool, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var requested bool
	var current int
	var jobState, attemptState string
	var verifier []byte
	var version int16
	var leaseValid bool
	err = tx.QueryRow(ctx, `SELECT j.cancel_requested_at IS NOT NULL,j.current_attempt_number,j.state,a.state,a.token_verifier,a.token_pepper_version,a.lease_expires_at>transaction_timestamp()
		FROM jobs j JOIN job_attempts a ON a.job_id=j.id AND a.attempt_number=$2 WHERE j.id=$1`, jobID, attempt).Scan(&requested, &current, &jobState, &attemptState, &verifier, &version, &leaseValid)
	if err != nil || current != attempt || !s.workers.Verify(version, token, verifier) {
		return false, ErrLeaseLost
	}
	if !requested && (jobState != "running" || attemptState != "running" || !leaseValid) {
		return false, ErrLeaseLost
	}
	return requested, tx.Commit(ctx)
}

func (s *Service) AuthorizeAttempt(ctx context.Context, jobID string, attempt int, token string) (string, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return "", err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	current, err := s.checkAttempt(ctx, tx, jobID, attempt, token)
	if err != nil {
		return "", err
	}
	return current.workspaceID, tx.Commit(ctx)
}

func (s *Service) InputArtifact(ctx context.Context, jobID string, attempt int, token, artifactID string) (InputArtifact, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return InputArtifact{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if _, err = s.checkAttempt(ctx, tx, jobID, attempt, token); err != nil {
		return InputArtifact{}, err
	}
	var value InputArtifact
	err = tx.QueryRow(ctx, `SELECT r.id,r.bundle_object_key,r.bundle_object_version,r.declared_bundle_digest,r.declared_bundle_size FROM jobs j JOIN job_inputs i ON i.id=j.input_id JOIN runs r ON r.id IN(i.run_a_id,i.run_b_id) WHERE j.id=$1 AND r.id=$2`, jobID, artifactID).Scan(&value.ArtifactID, &value.ObjectKey, &value.ObjectVersion, &value.Digest, &value.Size)
	if err != nil {
		return value, ErrNotFound
	}
	value.MediaType = "application/x-tar"
	return value, tx.Commit(ctx)
}

// currentAttempt is the state a worker request is judged against: who owns the
// job, what it asked for, and how long the job has been alive.
type currentAttempt struct {
	workspaceID string
	operation   string
	age         time.Duration
}

func (s *Service) checkAttempt(ctx context.Context, tx pgx.Tx, jobID string, attempt int, token string) (currentAttempt, error) {
	var value currentAttempt
	var jobState, attemptState string
	var current int
	var verifier []byte
	var version int16
	var lease time.Time
	var ageSeconds float64
	err := tx.QueryRow(ctx, `SELECT j.workspace_id,i.operation,j.state,j.current_attempt_number,a.state,a.token_verifier,a.token_pepper_version,a.lease_expires_at,EXTRACT(EPOCH FROM (transaction_timestamp()-j.created_at))
		FROM jobs j JOIN job_inputs i ON i.id=j.input_id JOIN job_attempts a ON a.job_id=j.id AND a.attempt_number=$2 WHERE j.id=$1 AND a.lease_expires_at>transaction_timestamp() FOR UPDATE`, jobID, attempt).Scan(&value.workspaceID, &value.operation, &jobState, &current, &attemptState, &verifier, &version, &lease, &ageSeconds)
	if err != nil || jobState != "running" || attemptState != "running" || current != attempt || !s.workers.Verify(version, token, verifier) {
		return currentAttempt{}, ErrLeaseLost
	}
	value.age = time.Duration(ageSeconds * float64(time.Second))
	return value, nil
}

func (s *Service) Heartbeat(ctx context.Context, jobID string, attempt int, token string) (time.Time, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return time.Time{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if _, err = s.checkAttempt(ctx, tx, jobID, attempt, token); err != nil {
		return time.Time{}, err
	}
	var lease time.Time
	err = tx.QueryRow(ctx, `UPDATE job_attempts SET heartbeat_at=transaction_timestamp(),lease_expires_at=transaction_timestamp()+interval '60 seconds' WHERE job_id=$1 AND attempt_number=$2 RETURNING lease_expires_at`, jobID, attempt).Scan(&lease)
	if err != nil {
		return time.Time{}, err
	}
	return lease, tx.Commit(ctx)
}

func (s *Service) UpdateProgress(ctx context.Context, jobID string, attempt int, token string, progress Progress) error {
	if progress.Sequence < 1 || len(progress.Message) < 1 || len(progress.Message) > 512 {
		return errors.New("invalid progress")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if _, err = s.checkAttempt(ctx, tx, jobID, attempt, token); err != nil {
		return err
	}
	var seq int64
	var storedAttempt int
	var stage, message string
	err = tx.QueryRow(ctx, "SELECT attempt_number,sequence,stage,message FROM progress_snapshots WHERE job_id=$1", jobID).Scan(&storedAttempt, &seq, &stage, &message)
	// Sequences are monotonic within one attempt. A later attempt starts its
	// own sequence and supersedes the snapshot; an earlier one is stale.
	if err == nil && storedAttempt == attempt {
		if progress.Sequence == seq {
			if stage == progress.Stage && message == progress.Message {
				return tx.Commit(ctx)
			}
			return ErrConflict
		}
		if progress.Sequence < seq {
			return ErrConflict
		}
	}
	if err == nil && storedAttempt > attempt {
		return ErrConflict
	}
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return err
	}
	_, err = tx.Exec(ctx, `INSERT INTO progress_snapshots(job_id,attempt_number,sequence,stage,message) VALUES($1,$2,$3,$4,$5)
        ON CONFLICT(job_id) DO UPDATE SET attempt_number=EXCLUDED.attempt_number,sequence=EXCLUDED.sequence,stage=EXCLUDED.stage,message=EXCLUDED.message,updated_at=transaction_timestamp()`, jobID, attempt, progress.Sequence, progress.Stage, progress.Message)
	if err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Service) RequestCancellation(ctx context.Context, principal Principal, jobID string) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var operation string
	var ageSeconds float64
	err = tx.QueryRow(ctx, `UPDATE jobs SET cancel_requested_at=COALESCE(cancel_requested_at,transaction_timestamp()),row_version=row_version+1 WHERE id=$1 AND workspace_id=$2 AND state IN('queued','running','retry_wait')
        RETURNING (SELECT operation FROM job_inputs WHERE id=jobs.input_id),EXTRACT(EPOCH FROM (transaction_timestamp()-jobs.created_at))`, jobID, principal.WorkspaceID).Scan(&operation, &ageSeconds)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	command, err := tx.Exec(ctx, `UPDATE job_attempts SET state='cancelled',finished_at=transaction_timestamp() WHERE job_id=$1 AND state='running'`, jobID)
	if err != nil {
		return err
	}
	fenced := command.RowsAffected()
	_, err = tx.Exec(ctx, `UPDATE jobs SET state='cancelled',terminal_at=transaction_timestamp(),retry_at=NULL,updated_at=transaction_timestamp(),row_version=row_version+1 WHERE id=$1 AND state IN('queued','running','retry_wait')`, jobID)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `INSERT INTO audit_records(workspace_id,aggregate_type,aggregate_id,event_type,actor_type,payload) VALUES($1,'job',$2,'job.cancelled','tenant','{}')`, principal.WorkspaceID, jobID)
	if err != nil {
		return err
	}
	if err = tx.Commit(ctx); err != nil {
		return err
	}
	for count := int64(0); count < fenced; count++ {
		s.metrics.AttemptFenced(ctx, "cancelled")
	}
	s.metrics.JobTerminal(ctx, operation, "cancelled", time.Duration(ageSeconds*float64(time.Second)))
	return nil
}

func (s *Service) FailAttempt(ctx context.Context, jobID string, attempt int, token, code, message string, retryable bool) (string, error) {
	if len(code) == 0 || len(code) > 64 || len(message) > 512 {
		return "", errors.New("invalid failure")
	}
	permanent := map[string]bool{"invalid_bundle": true, "unsupported_version": true, "invalid_result": true, "unauthorized_input": true, "cancelled": true}
	retryableCodes := map[string]bool{"artifact_commit_failed": true, "transient_dependency": true, "internal": true}
	if (!permanent[code] && !retryableCodes[code]) || retryable != retryableCodes[code] {
		return "", errors.New("failure retryability does not match policy")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return "", err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	current, err := s.checkAttempt(ctx, tx, jobID, attempt, token)
	if err != nil {
		return "", err
	}
	workspace, operation := current.workspaceID, current.operation
	if retryable && attempt < 3 {
		delay := firstBackoff
		if attempt == 2 {
			delay = secondBackoff
		}
		_, err = tx.Exec(ctx, `UPDATE job_attempts SET state='fenced',finished_at=transaction_timestamp(),failure_code=$3,failure_message=$4 WHERE job_id=$1 AND attempt_number=$2`, jobID, attempt, code, message)
		if err != nil {
			return "", err
		}
		_, err = tx.Exec(ctx, `UPDATE jobs SET state='retry_wait',current_attempt_number=NULL,retry_at=transaction_timestamp()+$2::interval,updated_at=transaction_timestamp(),row_version=row_version+1 WHERE id=$1`, jobID, delay.String())
		if err != nil {
			return "", err
		}
	} else {
		if retryable {
			code = "retry_exhausted"
		}
		_, err = tx.Exec(ctx, `UPDATE job_attempts SET state='failed',finished_at=transaction_timestamp(),failure_code=$3,failure_message=$4 WHERE job_id=$1 AND attempt_number=$2`, jobID, attempt, code, message)
		if err != nil {
			return "", err
		}
		_, err = tx.Exec(ctx, `UPDATE jobs SET state='failed',terminal_at=transaction_timestamp(),failure_code=$2,failure_message=$3,updated_at=transaction_timestamp(),row_version=row_version+1 WHERE id=$1`, jobID, code, message)
		if err != nil {
			return "", err
		}
		if operation == "validate" {
			_, err = tx.Exec(ctx, `UPDATE runs r SET state='invalid',failure_code=$2,failure_message=$3,row_version=row_version+1 FROM job_inputs i WHERE i.id=(SELECT input_id FROM jobs WHERE id=$1) AND r.id=i.run_a_id AND r.state='validating'`, jobID, code, message)
			if err != nil {
				return "", err
			}
		}
	}
	_, err = tx.Exec(ctx, `INSERT INTO audit_records(workspace_id,aggregate_type,aggregate_id,event_type,actor_type,payload) VALUES($1,'job',$2,'attempt.failed','worker',jsonb_build_object('attempt',$3::integer,'failure_code',$4::text))`, workspace, jobID, attempt, code)
	if err != nil {
		return "", err
	}
	if err = tx.Commit(ctx); err != nil {
		return "", err
	}
	if retryable && attempt < 3 {
		s.metrics.AttemptFenced(ctx, "retryable_failure")
		return "retry_wait", nil
	}
	s.metrics.AttemptFenced(ctx, code)
	s.metrics.JobTerminal(ctx, operation, "failed", current.age)
	return "failed", nil
}

func (s *Service) CompleteAttempt(ctx context.Context, jobID string, attempt int, token string, result Completion) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	current, err := s.checkAttempt(ctx, tx, jobID, attempt, token)
	if err != nil {
		return err
	}
	workspace, operation := current.workspaceID, current.operation
	if result.ArtifactID == "" {
		result.ArtifactID, err = newID()
		if err != nil {
			return err
		}
	}
	expectedKinds := map[string]string{"validate": "validation_json", "diff": "diff_json", "otlp": "otlp_result_json", "pprof": "pprof_result_json"}
	if result.Kind != expectedKinds[operation] || result.SchemaName != "result-envelope" || result.SchemaVersion != 1 {
		return errors.New("result schema does not match operation")
	}
	expectedPrefix := "workspaces/" + workspace + "/jobs/" + jobID + "/attempts/" + strconv.Itoa(attempt) + "/"
	if !strings.HasPrefix(result.ObjectKey, expectedPrefix) {
		return errors.New("artifact key is outside the current attempt")
	}
	for _, companion := range result.Companions {
		if !strings.HasPrefix(companion.ObjectKey, expectedPrefix) {
			return errors.New("companion artifact key is outside the current attempt")
		}
		_, err = tx.Exec(ctx, `INSERT INTO artifacts(id,workspace_id,job_id,attempt_number,kind,object_key,object_version,digest,size,media_type,schema_name,schema_version,authoritative,retention_expires_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,true,transaction_timestamp()+interval '90 days')`, companion.ArtifactID, workspace, jobID, attempt, companion.Kind, companion.ObjectKey, companion.ObjectVersion, companion.Digest, companion.Size, companion.MediaType, companion.SchemaName, companion.SchemaVersion)
		if err != nil {
			return fmt.Errorf("register companion artifact: %w", err)
		}
	}
	_, err = tx.Exec(ctx, `INSERT INTO artifacts(id,workspace_id,job_id,attempt_number,kind,object_key,object_version,digest,size,media_type,schema_name,schema_version,authoritative,retention_expires_at)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,true,transaction_timestamp()+interval '90 days')`, result.ArtifactID, workspace, jobID, attempt, result.Kind, result.ObjectKey, result.ObjectVersion, result.Digest, result.Size, result.MediaType, result.SchemaName, result.SchemaVersion)
	if err != nil {
		return fmt.Errorf("register artifact: %w", err)
	}
	if operation == "validate" {
		command, updateErr := tx.Exec(ctx, `UPDATE runs r SET state='ready',validated_bundle_format=$2,cassette_format_version=$3,event_schema_version=$4,logical_run_digest=$5,event_count=$6,ready_at=transaction_timestamp(),row_version=row_version+1 FROM job_inputs i WHERE i.id=(SELECT input_id FROM jobs WHERE id=$1) AND r.id=i.run_a_id AND r.state='validating' AND r.declared_bundle_digest=$7`, jobID, result.BundleFormat, result.CassetteFormat, result.EventSchema, result.LogicalDigest, result.EventCount, result.BundleDigest)
		err = updateErr
		if err != nil {
			return err
		}
		if command.RowsAffected() != 1 {
			return errors.New("validated bundle digest did not match one validating run")
		}
	}
	_, err = tx.Exec(ctx, `UPDATE job_attempts SET state='succeeded',finished_at=transaction_timestamp() WHERE job_id=$1 AND attempt_number=$2`, jobID, attempt)
	if err != nil {
		return err
	}
	command, err := tx.Exec(ctx, `UPDATE jobs SET state='succeeded',terminal_at=transaction_timestamp(),result_artifact_id=$2,result_digest=$3,result_size=$4,result_schema_name=$5,result_schema_version=$6,updated_at=transaction_timestamp(),row_version=row_version+1 WHERE id=$1 AND state='running' AND current_attempt_number=$7`, jobID, result.ArtifactID, result.Digest, result.Size, result.SchemaName, result.SchemaVersion, attempt)
	if err != nil {
		return err
	}
	if command.RowsAffected() != 1 {
		return ErrLeaseLost
	}
	if _, err = tx.Exec(ctx, `INSERT INTO audit_records(workspace_id,aggregate_type,aggregate_id,event_type,actor_type,payload) VALUES($1,'job',$2,'job.succeeded','worker',jsonb_build_object('attempt',$3::integer,'artifact_id',$4::text))`, workspace, jobID, attempt, result.ArtifactID); err != nil {
		return err
	}
	if err = tx.Commit(ctx); err != nil {
		return err
	}
	s.metrics.ArtifactCommitted(ctx, result.Kind)
	for _, companion := range result.Companions {
		s.metrics.ArtifactCommitted(ctx, companion.Kind)
	}
	s.metrics.JobTerminal(ctx, operation, "succeeded", current.age)
	return nil
}
