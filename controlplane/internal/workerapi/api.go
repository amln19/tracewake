package workerapi

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/amln19/tracewake/controlplane/internal/artifacts"
	"github.com/amln19/tracewake/controlplane/internal/controlplane"
	"github.com/amln19/tracewake/controlplane/internal/telemetry"
)

type API struct {
	service   *controlplane.Service
	artifacts artifacts.Store
	baseURL   string
	metrics   *telemetry.Metrics
}

func New(service *controlplane.Service, artifactStore artifacts.Store, baseURL string) *API {
	return &API{service: service, artifacts: artifactStore, baseURL: baseURL, metrics: telemetry.NoMetrics()}
}

// UseTelemetry replaces the recorder this surface reports requests to.
func (a *API) UseTelemetry(metrics *telemetry.Metrics) { a.metrics = metrics }

func (a *API) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /internal/v1/identity", a.identity)
	mux.HandleFunc("GET /internal/v1/notifications/next", a.next)
	mux.HandleFunc("POST /internal/v1/notifications/{id}/ack", a.ack)
	mux.HandleFunc("POST /internal/v1/claims", a.claim)
	mux.HandleFunc("PUT /internal/v1/jobs/{job}/attempts/{attempt}/heartbeat", a.heartbeat)
	mux.HandleFunc("PUT /internal/v1/jobs/{job}/attempts/{attempt}/progress", a.progress)
	mux.HandleFunc("GET /internal/v1/jobs/{job}/attempts/{attempt}/cancellation", a.cancellation)
	mux.HandleFunc("POST /internal/v1/jobs/{job}/attempts/{attempt}/fail", a.fail)
	mux.HandleFunc("POST /internal/v1/jobs/{job}/attempts/{attempt}/artifacts", a.declareArtifact)
	mux.HandleFunc("POST /internal/v1/jobs/{job}/attempts/{attempt}/complete", a.complete)
	mux.HandleFunc("GET /internal/v1/jobs/{job}/attempts/{attempt}/inputs/{artifact}", a.input)
	return a.metrics.Instrument("worker", mux)
}

func (a *API) worker(w http.ResponseWriter, r *http.Request) (string, bool) {
	header := r.Header.Get("Authorization")
	if !strings.HasPrefix(header, "Bearer ") {
		writeError(w, http.StatusUnauthorized, "unauthenticated")
		return "", false
	}
	id, err := a.service.AuthenticateWorker(r.Context(), strings.TrimPrefix(header, "Bearer "))
	if err != nil {
		writeError(w, http.StatusUnauthorized, "unauthenticated")
		return "", false
	}
	return id, true
}

// identity lets a worker that received only a credential learn the worker ID
// its claims must carry.
func (a *API) identity(w http.ResponseWriter, r *http.Request) {
	worker, ok := a.worker(w, r)
	if !ok {
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"protocol_version": 1, "worker_id": worker})
}

func (a *API) next(w http.ResponseWriter, r *http.Request) {
	if _, ok := a.worker(w, r); !ok {
		return
	}
	item, err := a.service.NextNotification(r.Context())
	if err != nil {
		trace.SpanFromContext(r.Context()).SetAttributes(attribute.Bool(telemetry.IdleAttribute, true))
		w.WriteHeader(http.StatusNoContent)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"notification_id": item.ID, "notification": item.Payload})
}
func (a *API) ack(w http.ResponseWriter, r *http.Request) {
	if _, ok := a.worker(w, r); !ok {
		return
	}
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil || a.service.AcknowledgeNotification(r.Context(), id) != nil {
		writeError(w, http.StatusBadRequest, "invalid_request")
		return
	}
	w.WriteHeader(http.StatusNoContent)
}
func (a *API) claim(w http.ResponseWriter, r *http.Request) {
	worker, ok := a.worker(w, r)
	if !ok {
		return
	}
	var body struct {
		ProtocolVersion int    `json:"protocol_version"`
		WorkerID        string `json:"worker_id"`
		Notification    struct {
			ProtocolVersion int    `json:"protocol_version"`
			JobID           string `json:"job_id"`
			JobVersion      int64  `json:"job_version"`
			Operation       string `json:"operation"`
			Traceparent     string `json:"traceparent"`
		} `json:"notification"`
	}
	if decode(w, r, &body) != nil || body.ProtocolVersion != 1 || body.Notification.ProtocolVersion != 1 || body.WorkerID != worker {
		writeError(w, http.StatusUnprocessableEntity, "invalid_request")
		return
	}
	claim, err := a.service.ClaimNotification(r.Context(), worker, body.Notification.JobID, body.Notification.JobVersion, body.Notification.Traceparent)
	if err != nil {
		writeError(w, http.StatusConflict, "conflict")
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"protocol_version": 1, "job_id": claim.JobID, "attempt_number": claim.Attempt, "attempt_token": claim.AttemptToken, "lease_expires_at": claim.LeaseExpires, "operation": claim.Operation, "profile": claim.Profile, "input_artifacts": claim.Inputs})
}
func attemptNumber(r *http.Request) (int, error) { return strconv.Atoi(r.PathValue("attempt")) }
func (a *API) heartbeat(w http.ResponseWriter, r *http.Request) {
	if _, ok := a.worker(w, r); !ok {
		return
	}
	attempt, err := attemptNumber(r)
	if err != nil {
		writeError(w, 400, "invalid_request")
		return
	}
	var body struct {
		ProtocolVersion int    `json:"protocol_version"`
		Attempt         int    `json:"attempt_number"`
		Observed        string `json:"observed_lease_expires_at"`
	}
	if decode(w, r, &body) != nil || body.ProtocolVersion != 1 || body.Attempt != attempt {
		writeError(w, 400, "invalid_request")
		return
	}
	lease, err := a.service.Heartbeat(r.Context(), r.PathValue("job"), attempt, r.Header.Get("Tracewake-Attempt-Token"))
	if err != nil {
		writeError(w, 409, "lease_lost")
		return
	}
	w.Header().Set("Tracewake-Lease-Expires-At", lease.Format("2006-01-02T15:04:05.999999999Z07:00"))
	w.WriteHeader(http.StatusNoContent)
}
func (a *API) progress(w http.ResponseWriter, r *http.Request) {
	if _, ok := a.worker(w, r); !ok {
		return
	}
	attempt, err := attemptNumber(r)
	if err != nil {
		writeError(w, 400, "invalid_request")
		return
	}
	var body struct {
		ProtocolVersion int   `json:"protocol_version"`
		Attempt         int   `json:"attempt_number"`
		Sequence        int64 `json:"sequence"`
		Stage, Message  string
	}
	if decode(w, r, &body) != nil || body.ProtocolVersion != 1 || body.Attempt != attempt {
		writeError(w, 400, "invalid_request")
		return
	}
	err = a.service.UpdateProgress(r.Context(), r.PathValue("job"), attempt, r.Header.Get("Tracewake-Attempt-Token"), controlplane.Progress{Sequence: body.Sequence, Stage: body.Stage, Message: body.Message})
	if err != nil {
		writeError(w, 409, "lease_lost")
		return
	}
	w.WriteHeader(http.StatusNoContent)
}
func (a *API) cancellation(w http.ResponseWriter, r *http.Request) {
	if _, ok := a.worker(w, r); !ok {
		return
	}
	attempt, err := attemptNumber(r)
	if err != nil {
		writeError(w, 400, "invalid_request")
		return
	}
	cancelled, err := a.service.Cancellation(r.Context(), r.PathValue("job"), attempt, r.Header.Get("Tracewake-Attempt-Token"))
	if err != nil {
		writeError(w, 409, "lease_lost")
		return
	}
	writeJSON(w, 200, map[string]any{"protocol_version": 1, "cancel_requested": cancelled})
}
func (a *API) fail(w http.ResponseWriter, r *http.Request) {
	if _, ok := a.worker(w, r); !ok {
		return
	}
	attempt, err := attemptNumber(r)
	if err != nil {
		writeError(w, 400, "invalid_request")
		return
	}
	var body struct {
		SchemaVersion int `json:"schema_version"`
		Code, Message string
		Retryable     bool
	}
	if decode(w, r, &body) != nil || body.SchemaVersion != 1 {
		writeError(w, 400, "invalid_request")
		return
	}
	state, err := a.service.FailAttempt(r.Context(), r.PathValue("job"), attempt, r.Header.Get("Tracewake-Attempt-Token"), body.Code, body.Message, body.Retryable)
	if err != nil {
		writeError(w, 409, "lease_lost")
		return
	}
	status := http.StatusOK
	if state == "retry_wait" {
		status = http.StatusAccepted
	}
	writeJSON(w, status, map[string]string{"state": state})
}
func (a *API) declareArtifact(w http.ResponseWriter, r *http.Request) {
	if _, ok := a.worker(w, r); !ok {
		return
	}
	attempt, err := attemptNumber(r)
	if err != nil {
		writeError(w, 400, "invalid_request")
		return
	}
	var body struct {
		ProtocolVersion int    `json:"protocol_version"`
		Attempt         int    `json:"attempt_number"`
		Kind            string `json:"kind"`
		MediaType       string `json:"media_type"`
		Digest          string `json:"digest"`
		Size            int64  `json:"size"`
	}
	allowed := map[string]bool{"validation_json": true, "diff_json": true, "diff_html": true, "otlp_json": true, "otlp_result_json": true, "pprof": true, "pprof_result_json": true, "worker_diagnostic": true}
	if decode(w, r, &body) != nil || body.ProtocolVersion != 1 || body.Attempt != attempt || !allowed[body.Kind] || body.MediaType == "" {
		writeError(w, 400, "invalid_request")
		return
	}
	if body.Size > artifacts.MaxResultSize {
		writeError(w, 413, "invalid_request")
		return
	}
	workspace, err := a.service.AuthorizeAttempt(r.Context(), r.PathValue("job"), attempt, r.Header.Get("Tracewake-Attempt-Token"))
	if err != nil {
		writeError(w, 409, "lease_lost")
		return
	}
	key := "workspaces/" + workspace + "/jobs/" + r.PathValue("job") + "/attempts/" + strconv.Itoa(attempt) + "/" + body.Kind
	grant, err := a.artifacts.PutGrant(r.Context(), key, body.Digest, body.Size, body.MediaType)
	if err != nil {
		writeError(w, 400, "invalid_request")
		return
	}
	writeJSON(w, 201, map[string]any{
		"protocol_version": 1,
		"object_key":       key,
		"required_digest":  body.Digest,
		"required_size":    body.Size,
		"upload_url":       artifacts.Absolute(a.baseURL, grant.URL),
		"upload_method":    grant.Method,
		"upload_headers":   grant.Headers,
		"expires_at":       grant.ExpiresAt,
	})
}
func (a *API) complete(w http.ResponseWriter, r *http.Request) {
	if _, ok := a.worker(w, r); !ok {
		return
	}
	attempt, err := attemptNumber(r)
	if err != nil {
		writeError(w, 400, "invalid_request")
		return
	}
	var body controlplane.Completion
	if decode(w, r, &body) != nil {
		writeError(w, 400, "invalid_request")
		return
	}
	ctx, span := telemetry.Span(r.Context(), "artifact.commit", trace.SpanKindInternal)
	object, err := a.artifacts.Commit(ctx, body.ObjectKey, body.ObjectVersion, body.Digest, body.Size)
	if err == nil {
		body.ObjectVersion = object.Version
		for index, companion := range body.Companions {
			committed, commitErr := a.artifacts.Commit(ctx, companion.ObjectKey, companion.ObjectVersion, companion.Digest, companion.Size)
			if commitErr != nil {
				err = commitErr
				break
			}
			body.Companions[index].ObjectVersion = committed.Version
		}
	}
	span.End()
	if err != nil {
		a.metrics.ArtifactCommitFailed(ctx)
		writeError(w, 409, "artifact_commit_failed")
		return
	}
	err = a.service.CompleteAttempt(ctx, r.PathValue("job"), attempt, r.Header.Get("Tracewake-Attempt-Token"), body)
	if err != nil {
		writeError(w, 409, "lease_lost")
		return
	}
	writeJSON(w, 200, map[string]any{"protocol_version": 1, "status": "succeeded"})
}

func (a *API) input(w http.ResponseWriter, r *http.Request) {
	if _, ok := a.worker(w, r); !ok {
		return
	}
	attempt, err := attemptNumber(r)
	if err != nil {
		writeError(w, 400, "invalid_request")
		return
	}
	value, err := a.service.InputArtifact(r.Context(), r.PathValue("job"), attempt, r.Header.Get("Tracewake-Attempt-Token"), r.PathValue("artifact"))
	if err != nil {
		writeError(w, 409, "lease_lost")
		return
	}
	grant, err := a.artifacts.GetGrant(r.Context(), value.ObjectKey, value.ObjectVersion, value.MediaType)
	if err != nil {
		writeError(w, 500, "internal")
		return
	}
	writeJSON(w, 200, map[string]any{
		"protocol_version": 1,
		"artifact_id":      value.ArtifactID,
		"object_key":       value.ObjectKey,
		"object_version":   value.ObjectVersion,
		"digest":           value.Digest,
		"size":             value.Size,
		"media_type":       value.MediaType,
		"download_url":     artifacts.Absolute(a.baseURL, grant.URL),
		"expires_at":       grant.ExpiresAt,
	})
}
func decode(w http.ResponseWriter, r *http.Request, value any) error {
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(value); err != nil {
		return err
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		return errors.New("request must contain one JSON value")
	}
	return nil
}
func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
func writeError(w http.ResponseWriter, status int, code string) {
	writeJSON(w, status, map[string]any{"error": map[string]string{"code": code, "message": "request could not be completed"}})
}
