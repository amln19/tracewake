package controlplane

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
)

type RunView struct {
	ID               string       `json:"run_id"`
	State            string       `json:"state"`
	BundleDigest     string       `json:"bundle_digest"`
	BundleFormat     int          `json:"bundle_format_version"`
	LogicalDigest    *string      `json:"logical_run_digest"`
	CassetteFormat   *int         `json:"cassette_format_version"`
	EventSchema      *int         `json:"event_schema_version"`
	EventCount       *int         `json:"event_count"`
	Failure          *FailureView `json:"failure"`
	CreatedAt        time.Time    `json:"created_at"`
	RetentionExpires time.Time    `json:"retention_expires_at"`
	ReadyAt          *time.Time   `json:"ready_at"`
}
type AttemptView struct {
	Number     int          `json:"attempt_number"`
	State      string       `json:"state"`
	StartedAt  time.Time    `json:"started_at"`
	FinishedAt *time.Time   `json:"finished_at"`
	Failure    *FailureView `json:"failure"`
}
type JobView struct {
	ID              string           `json:"job_id"`
	Operation       string           `json:"operation"`
	State           string           `json:"state"`
	RunIDs          []string         `json:"run_ids"`
	Profile         *string          `json:"profile"`
	CurrentAttempt  *int             `json:"current_attempt_number"`
	Attempts        []AttemptView    `json:"attempts"`
	Progress        *Progress        `json:"progress"`
	CancelRequested *time.Time       `json:"cancel_requested_at"`
	Failure         *FailureView     `json:"failure"`
	CreatedAt       time.Time        `json:"created_at"`
	UpdatedAt       time.Time        `json:"updated_at"`
	TerminalAt      *time.Time       `json:"terminal_at"`
	Artifacts       []PublicArtifact `json:"artifacts"`
}
type FailureView struct {
	SchemaVersion int    `json:"schema_version"`
	Code          string `json:"code"`
	Message       string `json:"message"`
	Retryable     bool   `json:"retryable"`
}
type PublicArtifact struct {
	ID               string    `json:"artifact_id"`
	Kind             string    `json:"kind"`
	Digest           string    `json:"digest"`
	Size             int64     `json:"size"`
	MediaType        string    `json:"media_type"`
	SchemaName       *string   `json:"schema_name"`
	SchemaVersion    *int      `json:"schema_version"`
	RetentionExpires time.Time `json:"retention_expires_at"`
}
type ArtifactView struct {
	ID        string `json:"artifact_id"`
	Kind      string `json:"kind"`
	Key       string `json:"-"`
	Version   string `json:"-"`
	Digest    string `json:"digest"`
	MediaType string `json:"media_type"`
	Size      int64  `json:"size"`
}
type AuditView struct {
	ID            int64     `json:"id"`
	AggregateType string    `json:"aggregate_type"`
	AggregateID   string    `json:"aggregate_id"`
	EventType     string    `json:"event_type"`
	ActorType     string    `json:"actor_type"`
	CreatedAt     time.Time `json:"created_at"`
}
type RunPage struct {
	Items      []RunView
	NextCursor string
}
type AuditPage struct {
	Items      []AuditView
	NextCursor string
}
type listCursor struct {
	Kind      string    `json:"kind"`
	Workspace string    `json:"workspace"`
	CreatedAt time.Time `json:"created_at"`
	ID        string    `json:"id"`
}

func encodeListCursor(kind, workspace string, createdAt time.Time, id string) string {
	raw, _ := json.Marshal(listCursor{Kind: kind, Workspace: workspace, CreatedAt: createdAt, ID: id})
	return base64.RawURLEncoding.EncodeToString(raw)
}

func decodeListCursor(raw, kind, workspace string) (listCursor, error) {
	var cursor listCursor
	decoded, err := base64.RawURLEncoding.DecodeString(raw)
	if err != nil || json.Unmarshal(decoded, &cursor) != nil || cursor.Kind != kind || cursor.Workspace != workspace || cursor.CreatedAt.IsZero() || cursor.ID == "" {
		return listCursor{}, fmt.Errorf("%w: invalid list cursor", ErrInvalidRequest)
	}
	return cursor, nil
}

func (s *Service) GetRun(ctx context.Context, p Principal, id string) (RunView, error) {
	var v RunView
	var failureCode, failureMessage *string
	err := s.pool.QueryRow(ctx, `SELECT id,state,declared_bundle_format,declared_bundle_digest,logical_run_digest,cassette_format_version,event_schema_version,event_count,failure_code,failure_message,created_at,ready_at,retention_expires_at FROM runs WHERE id=$1 AND workspace_id=$2 AND state<>'deleted'`, id, p.WorkspaceID).Scan(&v.ID, &v.State, &v.BundleFormat, &v.BundleDigest, &v.LogicalDigest, &v.CassetteFormat, &v.EventSchema, &v.EventCount, &failureCode, &failureMessage, &v.CreatedAt, &v.ReadyAt, &v.RetentionExpires)
	if errors.Is(err, pgx.ErrNoRows) {
		return v, ErrNotFound
	}
	v.Failure = failureView(failureCode, failureMessage)
	return v, err
}
func (s *Service) ListRuns(ctx context.Context, p Principal, limit int) ([]RunView, error) {
	page, err := s.ListRunPage(ctx, p, "", limit)
	return page.Items, err
}
func (s *Service) ListRunPage(ctx context.Context, p Principal, cursorValue string, limit int) (RunPage, error) {
	if limit < 1 || limit > 100 {
		limit = 100
	}
	query := `SELECT id,state,declared_bundle_format,declared_bundle_digest,logical_run_digest,cassette_format_version,event_schema_version,event_count,failure_code,failure_message,created_at,ready_at,retention_expires_at FROM runs WHERE workspace_id=$1 AND state<>'deleted'`
	args := []any{p.WorkspaceID, limit + 1}
	if cursorValue != "" {
		cursor, err := decodeListCursor(cursorValue, "runs", p.WorkspaceID)
		if err != nil {
			return RunPage{}, err
		}
		query += ` AND (created_at,id)<($2,$3::uuid) ORDER BY created_at DESC,id DESC LIMIT $4`
		args = []any{p.WorkspaceID, cursor.CreatedAt, cursor.ID, limit + 1}
	} else {
		query += ` ORDER BY created_at DESC,id DESC LIMIT $2`
	}
	rows, err := s.pool.Query(ctx, query, args...)
	if err != nil {
		return RunPage{}, err
	}
	defer rows.Close()
	result := []RunView{}
	for rows.Next() {
		var v RunView
		var failureCode, failureMessage *string
		if err := rows.Scan(&v.ID, &v.State, &v.BundleFormat, &v.BundleDigest, &v.LogicalDigest, &v.CassetteFormat, &v.EventSchema, &v.EventCount, &failureCode, &failureMessage, &v.CreatedAt, &v.ReadyAt, &v.RetentionExpires); err != nil {
			return RunPage{}, err
		}
		v.Failure = failureView(failureCode, failureMessage)
		result = append(result, v)
	}
	if err := rows.Err(); err != nil {
		return RunPage{}, err
	}
	page := RunPage{Items: result}
	if len(result) > limit {
		page.Items = result[:limit]
		last := page.Items[len(page.Items)-1]
		page.NextCursor = encodeListCursor("runs", p.WorkspaceID, last.CreatedAt, last.ID)
	}
	return page, nil
}
func (s *Service) GetJob(ctx context.Context, p Principal, id string) (JobView, error) {
	v := JobView{Attempts: []AttemptView{}, Artifacts: []PublicArtifact{}}
	var runA string
	var runB *string
	var failureCode, failureMessage *string
	err := s.pool.QueryRow(ctx, `SELECT j.id,i.operation,i.run_a_id,i.run_b_id,i.analysis_profile,j.state,j.current_attempt_number,j.cancel_requested_at,j.failure_code,j.failure_message,j.created_at,j.updated_at,j.terminal_at FROM jobs j JOIN job_inputs i ON i.id=j.input_id WHERE j.id=$1 AND j.workspace_id=$2 AND i.operation<>'validate'`, id, p.WorkspaceID).Scan(&v.ID, &v.Operation, &runA, &runB, &v.Profile, &v.State, &v.CurrentAttempt, &v.CancelRequested, &failureCode, &failureMessage, &v.CreatedAt, &v.UpdatedAt, &v.TerminalAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return v, ErrNotFound
	}
	if err != nil {
		return v, err
	}
	v.Failure = failureView(failureCode, failureMessage)
	v.RunIDs = []string{runA}
	if runB != nil {
		v.RunIDs = append(v.RunIDs, *runB)
	}
	rows, err := s.pool.Query(ctx, `SELECT attempt_number,state,started_at,finished_at,failure_code,failure_message FROM job_attempts WHERE job_id=$1 ORDER BY attempt_number`, id)
	if err != nil {
		return v, err
	}
	for rows.Next() {
		var a AttemptView
		var attemptFailureCode, attemptFailureMessage *string
		if err := rows.Scan(&a.Number, &a.State, &a.StartedAt, &a.FinishedAt, &attemptFailureCode, &attemptFailureMessage); err != nil {
			rows.Close()
			return v, err
		}
		a.Failure = failureView(attemptFailureCode, attemptFailureMessage)
		v.Attempts = append(v.Attempts, a)
	}
	rows.Close()
	artifactRows, err := s.pool.Query(ctx, `SELECT id,kind,digest,size,media_type,schema_name,schema_version,retention_expires_at FROM artifacts WHERE job_id=$1 AND workspace_id=$2 AND authoritative AND retention_expires_at>transaction_timestamp() ORDER BY kind`, id, p.WorkspaceID)
	if err != nil {
		return v, err
	}
	for artifactRows.Next() {
		var artifact PublicArtifact
		if err := artifactRows.Scan(&artifact.ID, &artifact.Kind, &artifact.Digest, &artifact.Size, &artifact.MediaType, &artifact.SchemaName, &artifact.SchemaVersion, &artifact.RetentionExpires); err != nil {
			artifactRows.Close()
			return v, err
		}
		v.Artifacts = append(v.Artifacts, artifact)
	}
	artifactRows.Close()
	var progress Progress
	err = s.pool.QueryRow(ctx, `SELECT attempt_number,sequence,stage,message FROM progress_snapshots WHERE job_id=$1`, id).Scan(&progress.AttemptNumber, &progress.Sequence, &progress.Stage, &progress.Message)
	if err == nil {
		progress.ProtocolVersion = 1
		v.Progress = &progress
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return v, err
	}
	return v, nil
}
func (s *Service) GetArtifact(ctx context.Context, p Principal, id string) (ArtifactView, error) {
	var v ArtifactView
	err := s.pool.QueryRow(ctx, `SELECT id,kind,object_key,object_version,digest,size,media_type FROM artifacts WHERE id=$1 AND workspace_id=$2 AND authoritative AND retention_expires_at>transaction_timestamp()`, id, p.WorkspaceID).Scan(&v.ID, &v.Kind, &v.Key, &v.Version, &v.Digest, &v.Size, &v.MediaType)
	if errors.Is(err, pgx.ErrNoRows) {
		return v, ErrNotFound
	}
	return v, err
}
func (s *Service) ListAudit(ctx context.Context, p Principal, limit int) ([]AuditView, error) {
	page, err := s.ListAuditPage(ctx, p, "", limit)
	return page.Items, err
}
func (s *Service) ListAuditPage(ctx context.Context, p Principal, cursorValue string, limit int) (AuditPage, error) {
	if limit < 1 || limit > 100 {
		limit = 100
	}
	query := `SELECT id,aggregate_type,aggregate_id,event_type,actor_type,created_at FROM audit_records WHERE workspace_id=$1`
	args := []any{p.WorkspaceID, limit + 1}
	if cursorValue != "" {
		cursor, err := decodeListCursor(cursorValue, "audit", p.WorkspaceID)
		if err != nil {
			return AuditPage{}, err
		}
		id, err := strconv.ParseInt(cursor.ID, 10, 64)
		if err != nil {
			return AuditPage{}, fmt.Errorf("%w: invalid list cursor", ErrInvalidRequest)
		}
		query += ` AND (created_at,id)<($2,$3) ORDER BY created_at DESC,id DESC LIMIT $4`
		args = []any{p.WorkspaceID, cursor.CreatedAt, id, limit + 1}
	} else {
		query += ` ORDER BY created_at DESC,id DESC LIMIT $2`
	}
	rows, err := s.pool.Query(ctx, query, args...)
	if err != nil {
		return AuditPage{}, err
	}
	defer rows.Close()
	result := []AuditView{}
	for rows.Next() {
		var v AuditView
		if err := rows.Scan(&v.ID, &v.AggregateType, &v.AggregateID, &v.EventType, &v.ActorType, &v.CreatedAt); err != nil {
			return AuditPage{}, err
		}
		result = append(result, v)
	}
	if err := rows.Err(); err != nil {
		return AuditPage{}, err
	}
	page := AuditPage{Items: result}
	if len(result) > limit {
		page.Items = result[:limit]
		last := page.Items[len(page.Items)-1]
		page.NextCursor = encodeListCursor("audit", p.WorkspaceID, last.CreatedAt, strconv.FormatInt(last.ID, 10))
	}
	return page, nil
}
func (s *Service) CurrentProgress(ctx context.Context, p Principal, jobID string) (Progress, error) {
	var value Progress
	err := s.pool.QueryRow(ctx, `SELECT p.attempt_number,p.sequence,p.stage,p.message FROM progress_snapshots p JOIN jobs j ON j.id=p.job_id WHERE p.job_id=$1 AND j.workspace_id=$2`, jobID, p.WorkspaceID).Scan(&value.AttemptNumber, &value.Sequence, &value.Stage, &value.Message)
	if err != nil {
		return value, fmt.Errorf("read progress: %w", err)
	}
	value.ProtocolVersion = 1
	return value, nil
}

func failureView(code, message *string) *FailureView {
	if code == nil || message == nil {
		return nil
	}
	retryable := *code == "lease_lost" || *code == "artifact_commit_failed" || *code == "transient_dependency" || *code == "internal"
	return &FailureView{SchemaVersion: 1, Code: *code, Message: *message, Retryable: retryable}
}
