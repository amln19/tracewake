package workerapi

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"

	"github.com/amln19/locus/controlplane/internal/artifacts"
	"github.com/amln19/locus/controlplane/internal/controlplane"
)

type API struct {
	service   *controlplane.Service
	artifacts *artifacts.Store
}

func New(service *controlplane.Service, artifactStore *artifacts.Store) *API {
	return &API{service: service, artifacts: artifactStore}
}

func (a *API) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /internal/v1/notifications/next", a.next)
	mux.HandleFunc("POST /internal/v1/notifications/{id}/ack", a.ack)
	mux.HandleFunc("POST /internal/v1/claims", a.claim)
	mux.HandleFunc("PUT /internal/v1/jobs/{job}/attempts/{attempt}/heartbeat", a.heartbeat)
	mux.HandleFunc("PUT /internal/v1/jobs/{job}/attempts/{attempt}/progress", a.progress)
	mux.HandleFunc("GET /internal/v1/jobs/{job}/attempts/{attempt}/cancellation", a.cancellation)
	mux.HandleFunc("POST /internal/v1/jobs/{job}/attempts/{attempt}/fail", a.fail)
	mux.HandleFunc("PUT /internal/v1/jobs/{job}/attempts/{attempt}/artifacts/{kind}", a.putArtifact)
	mux.HandleFunc("POST /internal/v1/jobs/{job}/attempts/{attempt}/complete", a.complete)
	mux.HandleFunc("GET /internal/v1/jobs/{job}/attempts/{attempt}/inputs/{artifact}", a.input)
	return mux
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

func (a *API) next(w http.ResponseWriter, r *http.Request) {
	if _, ok := a.worker(w, r); !ok {
		return
	}
	item, err := a.service.NextNotification(r.Context())
	if err != nil {
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
		} `json:"notification"`
	}
	if decode(w, r, &body) != nil || body.ProtocolVersion != 1 || body.Notification.ProtocolVersion != 1 || body.WorkerID != worker {
		writeError(w, http.StatusUnprocessableEntity, "invalid_request")
		return
	}
	claim, err := a.service.ClaimNotification(r.Context(), worker, body.Notification.JobID, body.Notification.JobVersion)
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
	lease, err := a.service.Heartbeat(r.Context(), r.PathValue("job"), attempt, r.Header.Get("Locus-Attempt-Token"))
	if err != nil {
		writeError(w, 409, "lease_lost")
		return
	}
	w.Header().Set("Locus-Lease-Expires-At", lease.Format("2006-01-02T15:04:05.999999999Z07:00"))
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
	err = a.service.UpdateProgress(r.Context(), r.PathValue("job"), attempt, r.Header.Get("Locus-Attempt-Token"), controlplane.Progress{Sequence: body.Sequence, Stage: body.Stage, Message: body.Message})
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
	cancelled, err := a.service.Cancellation(r.Context(), r.PathValue("job"), attempt, r.Header.Get("Locus-Attempt-Token"))
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
	state, err := a.service.FailAttempt(r.Context(), r.PathValue("job"), attempt, r.Header.Get("Locus-Attempt-Token"), body.Code, body.Message, body.Retryable)
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
func (a *API) putArtifact(w http.ResponseWriter, r *http.Request) {
	if _, ok := a.worker(w, r); !ok {
		return
	}
	attempt, err := attemptNumber(r)
	if err != nil {
		writeError(w, 400, "invalid_request")
		return
	}
	workspace, err := a.service.AuthorizeAttempt(r.Context(), r.PathValue("job"), attempt, r.Header.Get("Locus-Attempt-Token"))
	if err != nil {
		writeError(w, 409, "lease_lost")
		return
	}
	kind := r.PathValue("kind")
	allowed := map[string]bool{"validation_json": true, "diff_json": true, "diff_html": true, "otlp_json": true, "pprof": true, "worker_diagnostic": true}
	if !allowed[kind] {
		writeError(w, 400, "invalid_request")
		return
	}
	digest := r.Header.Get("Locus-Artifact-Digest")
	size, err := strconv.ParseInt(r.Header.Get("Locus-Artifact-Size"), 10, 64)
	if err != nil {
		writeError(w, 400, "invalid_request")
		return
	}
	key := "workspaces/" + workspace + "/jobs/" + r.PathValue("job") + "/attempts/" + strconv.Itoa(attempt) + "/" + kind
	object, err := a.artifacts.Put(key, digest, size, http.MaxBytesReader(w, r.Body, size+1))
	if err != nil {
		writeError(w, 400, "invalid_request")
		return
	}
	writeJSON(w, 201, object)
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
	if err := a.artifacts.Verify(body.ObjectKey, body.ObjectVersion, body.Digest, body.Size); err != nil {
		writeError(w, 409, "artifact_commit_failed")
		return
	}
	for _, companion := range body.Companions {
		if err := a.artifacts.Verify(companion.ObjectKey, companion.ObjectVersion, companion.Digest, companion.Size); err != nil {
			writeError(w, 409, "artifact_commit_failed")
			return
		}
	}
	err = a.service.CompleteAttempt(r.Context(), r.PathValue("job"), attempt, r.Header.Get("Locus-Attempt-Token"), body)
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
	value, err := a.service.InputArtifact(r.Context(), r.PathValue("job"), attempt, r.Header.Get("Locus-Attempt-Token"), r.PathValue("artifact"))
	if err != nil {
		writeError(w, 409, "lease_lost")
		return
	}
	file, err := a.artifacts.Open(value.ObjectKey, value.ObjectVersion)
	if err != nil {
		writeError(w, 500, "internal")
		return
	}
	defer file.Close()
	w.Header().Set("Content-Type", value.MediaType)
	w.Header().Set("Content-Length", strconv.FormatInt(value.Size, 10))
	_, _ = io.Copy(w, file)
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
