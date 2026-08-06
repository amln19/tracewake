package controlplane

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"time"

	"github.com/jackc/pgx/v5"
)

const (
	leaseDuration = 60 * time.Second
	firstBackoff  = 5 * time.Second
	secondBackoff = 30 * time.Second
)

var (
	ErrConflict            = errors.New("conflict")
	ErrIdempotencyConflict = errors.New("idempotency conflict")
	ErrLeaseLost           = errors.New("lease lost")
	ErrNotFound            = errors.New("not found")
)

type JobRequest struct {
	Operation string   `json:"operation"`
	RunIDs    []string `json:"run_ids"`
	Profile   *string  `json:"profile"`
}

type Job struct {
	ID      string
	State   string
	Attempt *int
}

type Claim struct {
	JobID        string
	Attempt      int
	AttemptToken string
	LeaseExpires time.Time
	Operation    string
	Profile      *string
	Inputs       []InputArtifact
}

type InputArtifact struct {
	ArtifactID    string  `json:"artifact_id"`
	ObjectKey     string  `json:"object_key"`
	ObjectVersion string  `json:"object_version"`
	Digest        string  `json:"digest"`
	Size          int64   `json:"size"`
	MediaType     string  `json:"media_type"`
	SchemaName    *string `json:"schema_name"`
	SchemaVersion *int    `json:"schema_version"`
}

type Upload struct {
	RunID, Key, Digest string
	Size               int64
}

func (s *Service) CreateUpload(ctx context.Context, principal Principal, digest string, size int64) (Upload, error) {
	if len(digest) != 64 || size < 0 || size > 256*1024*1024 {
		return Upload{}, errors.New("upload declaration is invalid")
	}
	runID, err := newID()
	if err != nil {
		return Upload{}, err
	}
	key := "workspaces/" + principal.WorkspaceID + "/runs/" + runID + "/bundle.tar"
	if _, err := s.pool.Exec(ctx, `INSERT INTO runs (id,workspace_id,declared_bundle_format,declared_bundle_digest,declared_bundle_size,bundle_object_key)
        VALUES ($1,$2,1,$3,$4,$5)`, runID, principal.WorkspaceID, digest, size, key); err != nil {
		return Upload{}, fmt.Errorf("create upload: %w", err)
	}
	return Upload{RunID: runID, Key: key, Digest: digest, Size: size}, nil
}

func (s *Service) CompleteUpload(ctx context.Context, principal Principal, runID, version, digest string, size int64) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	command, err := tx.Exec(ctx, `UPDATE runs SET state='validating',bundle_object_version=$3,uploaded_at=transaction_timestamp(),row_version=row_version+1
        WHERE id=$1 AND workspace_id=$2 AND state='pending' AND declared_bundle_digest=$4 AND declared_bundle_size=$5`, runID, principal.WorkspaceID, version, digest, size)
	if err != nil {
		return fmt.Errorf("complete upload: %w", err)
	}
	if command.RowsAffected() != 1 {
		return ErrConflict
	}
	inputID, err := newID()
	if err != nil {
		return err
	}
	jobID, err := newID()
	if err != nil {
		return err
	}
	normalized, err := normalizedValidationDigest(runID)
	if err != nil {
		return err
	}
	if _, err := tx.Exec(ctx, `INSERT INTO job_inputs (id,workspace_id,operation,run_a_id,normalized_digest) VALUES ($1,$2,'validate',$3,$4)`, inputID, principal.WorkspaceID, runID, normalized); err != nil {
		return err
	}
	if _, err := tx.Exec(ctx, "INSERT INTO jobs (id,workspace_id,input_id) VALUES ($1,$2,$3)", jobID, principal.WorkspaceID, inputID); err != nil {
		return err
	}
	payload, _ := json.Marshal(map[string]any{"protocol_version": 1, "job_id": jobID, "job_version": 1, "operation": "validate"})
	if _, err := tx.Exec(ctx, `INSERT INTO outbox (aggregate_type,aggregate_id,aggregate_version,topic,payload) VALUES ('job',$1,1,'job.created',$2)`, jobID, payload); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Service) UploadFor(ctx context.Context, principal Principal, runID string) (Upload, error) {
	var upload Upload
	err := s.pool.QueryRow(ctx, `SELECT id,bundle_object_key,declared_bundle_digest,declared_bundle_size FROM runs WHERE id=$1 AND workspace_id=$2 AND state='pending'`, runID, principal.WorkspaceID).Scan(&upload.RunID, &upload.Key, &upload.Digest, &upload.Size)
	if errors.Is(err, pgx.ErrNoRows) {
		return Upload{}, ErrConflict
	}
	if err != nil {
		return Upload{}, fmt.Errorf("read upload: %w", err)
	}
	return upload, nil
}

func normalizedDigest(request JobRequest) (string, error) {
	if request.Operation != "diff" && request.Operation != "otlp" && request.Operation != "pprof" {
		return "", errors.New("unsupported job operation")
	}
	expected := 1
	if request.Operation == "diff" {
		expected = 2
		if request.Profile == nil || *request.Profile != "lexical-v1" {
			return "", errors.New("diff requires lexical-v1")
		}
	} else if request.Profile != nil {
		return "", errors.New("only diff accepts an analysis profile")
	}
	if len(request.RunIDs) != expected {
		return "", errors.New("job has invalid run identities")
	}
	if request.RunIDs[0] == "" || (expected == 2 && (request.RunIDs[1] == "" || request.RunIDs[0] == request.RunIDs[1])) {
		return "", errors.New("job has invalid run identities")
	}
	encoded, err := json.Marshal(request)
	if err != nil {
		return "", fmt.Errorf("encode normalized job input: %w", err)
	}
	digest := sha256.Sum256(encoded)
	return hex.EncodeToString(digest[:]), nil
}

func (s *Service) CreateValidationJob(ctx context.Context, workspaceID, runID string) (string, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return "", fmt.Errorf("begin validation job: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var uploaded bool
	if err := tx.QueryRow(ctx, "SELECT state = 'uploaded' FROM runs WHERE id=$1 AND workspace_id=$2 FOR UPDATE", runID, workspaceID).Scan(&uploaded); err != nil || !uploaded {
		return "", ErrConflict
	}
	inputID, err := newID()
	if err != nil {
		return "", err
	}
	jobID, err := newID()
	if err != nil {
		return "", err
	}
	digest, err := normalizedValidationDigest(runID)
	if err != nil {
		return "", err
	}
	if _, err := tx.Exec(ctx, `INSERT INTO job_inputs (id,workspace_id,operation,run_a_id,normalized_digest)
        VALUES ($1,$2,'validate',$3,$4)`, inputID, workspaceID, runID, digest); err != nil {
		return "", fmt.Errorf("insert validation input: %w", err)
	}
	if _, err := tx.Exec(ctx, "INSERT INTO jobs (id,workspace_id,input_id) VALUES ($1,$2,$3)", jobID, workspaceID, inputID); err != nil {
		return "", fmt.Errorf("insert validation job: %w", err)
	}
	if _, err := tx.Exec(ctx, "UPDATE runs SET state='validating',row_version=row_version+1 WHERE id=$1 AND state='uploaded'", runID); err != nil {
		return "", fmt.Errorf("mark run validating: %w", err)
	}
	payload, _ := json.Marshal(map[string]any{"protocol_version": 1, "job_id": jobID, "job_version": 1, "operation": "validate"})
	if _, err := tx.Exec(ctx, `INSERT INTO outbox (aggregate_type,aggregate_id,aggregate_version,topic,payload) VALUES ('job',$1,1,'job.created',$2)`, jobID, payload); err != nil {
		return "", fmt.Errorf("enqueue validation: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return "", fmt.Errorf("commit validation job: %w", err)
	}
	return jobID, nil
}

func normalizedValidationDigest(runID string) (string, error) {
	data, err := json.Marshal(struct {
		Operation string `json:"operation"`
		RunID     string `json:"run_id"`
	}{"validate", runID})
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(data)
	return hex.EncodeToString(digest[:]), nil
}

func (s *Service) CreateJob(ctx context.Context, principal Principal, key string, request JobRequest) (Job, bool, error) {
	if len(key) == 0 || len(key) > 255 {
		return Job{}, false, errors.New("idempotency key must contain 1 to 255 characters")
	}
	digest, err := normalizedDigest(request)
	if err != nil {
		return Job{}, false, err
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Job{}, false, fmt.Errorf("begin job transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var responseID, existingDigest string
	err = tx.QueryRow(ctx, `SELECT response_id, request_digest FROM idempotency_records
        WHERE workspace_id=$1 AND operation='create_job' AND idempotency_key=$2
          AND expires_at > transaction_timestamp()`, principal.WorkspaceID, key).Scan(&responseID, &existingDigest)
	if err == nil {
		if existingDigest != digest {
			return Job{}, false, ErrIdempotencyConflict
		}
		job, err := getJob(ctx, tx, principal.WorkspaceID, responseID)
		return job, true, err
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return Job{}, false, fmt.Errorf("read idempotency record: %w", err)
	}
	for _, runID := range request.RunIDs {
		var ready bool
		if err := tx.QueryRow(ctx, "SELECT state = 'ready' FROM runs WHERE id=$1 AND workspace_id=$2", runID, principal.WorkspaceID).Scan(&ready); err != nil || !ready {
			return Job{}, false, ErrConflict
		}
	}
	inputID, err := newID()
	if err != nil {
		return Job{}, false, err
	}
	jobID, err := newID()
	if err != nil {
		return Job{}, false, err
	}
	var runB *string
	if len(request.RunIDs) == 2 {
		runB = &request.RunIDs[1]
	}
	if _, err := tx.Exec(ctx, `INSERT INTO job_inputs (id,workspace_id,operation,run_a_id,run_b_id,analysis_profile,normalized_digest)
        VALUES ($1,$2,$3,$4,$5,$6,$7)`, inputID, principal.WorkspaceID, request.Operation, request.RunIDs[0], runB, request.Profile, digest); err != nil {
		return Job{}, false, fmt.Errorf("insert job input: %w", err)
	}
	if _, err := tx.Exec(ctx, "INSERT INTO jobs (id,workspace_id,input_id) VALUES ($1,$2,$3)", jobID, principal.WorkspaceID, inputID); err != nil {
		return Job{}, false, fmt.Errorf("insert job: %w", err)
	}
	payload, _ := json.Marshal(map[string]any{"protocol_version": 1, "job_id": jobID, "job_version": 1, "operation": request.Operation})
	if _, err := tx.Exec(ctx, `INSERT INTO outbox (aggregate_type,aggregate_id,aggregate_version,topic,payload)
        VALUES ('job',$1,1,'job.created',$2)`, jobID, payload); err != nil {
		return Job{}, false, fmt.Errorf("insert job outbox: %w", err)
	}
	if _, err := tx.Exec(ctx, `INSERT INTO audit_records (workspace_id,aggregate_type,aggregate_id,event_type,actor_type,payload)
        VALUES ($1,'job',$2,'job.created','tenant',$3)`, principal.WorkspaceID, jobID, []byte(`{"state":"queued"}`)); err != nil {
		return Job{}, false, fmt.Errorf("audit job creation: %w", err)
	}
	if _, err := tx.Exec(ctx, `INSERT INTO idempotency_records (workspace_id,operation,idempotency_key,request_digest,response_kind,response_id)
        VALUES ($1,'create_job',$2,$3,'job',$4)`, principal.WorkspaceID, key, digest, jobID); err != nil {
		return Job{}, false, fmt.Errorf("insert idempotency record: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return Job{}, false, fmt.Errorf("commit job transaction: %w", err)
	}
	return Job{ID: jobID, State: "queued"}, false, nil
}

func (s *Service) Claim(ctx context.Context, workerID, jobID string) (Claim, error) {
	return s.claim(ctx, workerID, jobID, 0)
}

func (s *Service) ClaimNotification(ctx context.Context, workerID, jobID string, jobVersion int64) (Claim, error) {
	return s.claim(ctx, workerID, jobID, jobVersion)
}

func (s *Service) claim(ctx context.Context, workerID, jobID string, expectedVersion int64) (Claim, error) {
	transaction, err := s.pool.Begin(ctx)
	if err != nil {
		return Claim{}, fmt.Errorf("begin claim: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()
	var state, operation string
	var profile *string
	var next int
	var rowVersion int64
	var workspaceID string
	err = transaction.QueryRow(ctx, `SELECT j.state, i.operation, i.analysis_profile, COALESCE((SELECT max(a.attempt_number) FROM job_attempts a WHERE a.job_id=j.id),0)+1,j.row_version,j.workspace_id
        FROM jobs j JOIN job_inputs i ON i.id=j.input_id
		WHERE j.id=$1 FOR UPDATE`, jobID).Scan(&state, &operation, &profile, &next, &rowVersion, &workspaceID)
	if errors.Is(err, pgx.ErrNoRows) {
		return Claim{}, ErrConflict
	}
	if err != nil {
		return Claim{}, fmt.Errorf("lock job: %w", err)
	}
	if state != "queued" || next > 3 || (expectedVersion > 0 && expectedVersion != rowVersion) {
		return Claim{}, ErrConflict
	}
	prefix, err := randomPrefix("attempt")
	if err != nil {
		return Claim{}, err
	}
	token, verifier, err := s.workers.NewToken(prefix)
	if err != nil {
		return Claim{}, err
	}
	var lease time.Time
	if err := transaction.QueryRow(ctx, "SELECT transaction_timestamp() + $1::interval", "60 seconds").Scan(&lease); err != nil {
		return Claim{}, fmt.Errorf("read database lease time: %w", err)
	}
	if _, err := transaction.Exec(ctx, `INSERT INTO job_attempts (job_id,attempt_number,worker_id,token_verifier,token_pepper_version,lease_expires_at)
		VALUES ($1,$2,$3,$4,$5,$6)`, jobID, next, workerID, verifier, s.workers.CurrentVersion, lease); err != nil {
		return Claim{}, fmt.Errorf("insert attempt: %w", err)
	}
	if _, err := transaction.Exec(ctx, `UPDATE jobs SET state='running',current_attempt_number=$2,retry_at=NULL,updated_at=transaction_timestamp(),row_version=row_version+1
        WHERE id=$1 AND state='queued'`, jobID, next); err != nil {
		return Claim{}, fmt.Errorf("mark job running: %w", err)
	}
	if _, err := transaction.Exec(ctx, `INSERT INTO audit_records(workspace_id,aggregate_type,aggregate_id,event_type,actor_type,actor_id,payload) VALUES($1,'job',$2,'attempt.claimed','worker',$3,jsonb_build_object('attempt',$4::integer))`, workspaceID, jobID, workerID, next); err != nil {
		return Claim{}, fmt.Errorf("audit claim: %w", err)
	}
	rows, err := transaction.Query(ctx, `SELECT r.id,r.bundle_object_key,r.bundle_object_version,r.declared_bundle_digest,r.declared_bundle_size FROM jobs j JOIN job_inputs i ON i.id=j.input_id JOIN runs r ON r.id IN(i.run_a_id,i.run_b_id) WHERE j.id=$1 ORDER BY CASE WHEN r.id=i.run_a_id THEN 0 ELSE 1 END`, jobID)
	if err != nil {
		return Claim{}, err
	}
	var inputs []InputArtifact
	for rows.Next() {
		var value InputArtifact
		if err := rows.Scan(&value.ArtifactID, &value.ObjectKey, &value.ObjectVersion, &value.Digest, &value.Size); err != nil {
			rows.Close()
			return Claim{}, err
		}
		value.MediaType = "application/x-tar"
		inputs = append(inputs, value)
	}
	rows.Close()
	if err := transaction.Commit(ctx); err != nil {
		return Claim{}, fmt.Errorf("commit claim: %w", err)
	}
	return Claim{JobID: jobID, Attempt: next, AttemptToken: token, LeaseExpires: lease, Operation: operation, Profile: profile, Inputs: inputs}, nil
}

func getJob(ctx context.Context, q interface {
	QueryRow(context.Context, string, ...any) pgx.Row
}, workspaceID, jobID string) (Job, error) {
	var job Job
	if err := q.QueryRow(ctx, "SELECT id,state,current_attempt_number FROM jobs WHERE id=$1 AND workspace_id=$2", jobID, workspaceID).Scan(&job.ID, &job.State, &job.Attempt); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return Job{}, ErrConflict
		}
		return Job{}, fmt.Errorf("read job: %w", err)
	}
	return job, nil
}

func sortedScopes(scopes map[string]bool) []string {
	items := make([]string, 0, len(scopes))
	for scope := range scopes {
		items = append(items, scope)
	}
	sort.Strings(items)
	return items
}
