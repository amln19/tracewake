package httpapi

import (
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/amln19/locus/controlplane/internal/artifacts"
	"github.com/amln19/locus/controlplane/internal/controlplane"
)

type API struct {
	service   *controlplane.Service
	artifacts *artifacts.Store
}

func New(service *controlplane.Service, artifacts *artifacts.Store) *API {
	return &API{service: service, artifacts: artifacts}
}
func (a *API) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/runs/uploads", a.createUpload)
	mux.HandleFunc("PUT /v1/local/uploads/{runID}", a.putUpload)
	mux.HandleFunc("GET /v1/runs", a.listRuns)
	mux.HandleFunc("GET /v1/runs/{runID}", a.getRun)
	mux.HandleFunc("POST /v1/jobs", a.createJob)
	mux.HandleFunc("GET /v1/jobs/{jobID}", a.getJob)
	mux.HandleFunc("POST /v1/jobs/{jobID}/cancel", a.cancelJob)
	mux.HandleFunc("GET /v1/jobs/{jobID}/events", a.events)
	mux.HandleFunc("GET /v1/artifacts/{artifactID}/download", a.download)
	mux.HandleFunc("GET /v1/audit", a.audit)
	return mux
}
func (a *API) putUpload(w http.ResponseWriter, r *http.Request) {
	principal, ok := a.principal(w, r, "runs:write")
	if !ok {
		return
	}
	upload, err := a.service.UploadFor(r.Context(), principal, r.PathValue("runID"))
	if err != nil {
		errorJSON(w, http.StatusNotFound, "not_found")
		return
	}
	object, err := a.artifacts.Put(upload.Key, upload.Digest, upload.Size, http.MaxBytesReader(w, r.Body, upload.Size+1))
	if err != nil {
		errorJSON(w, http.StatusBadRequest, "invalid_request")
		return
	}
	if err := a.service.CompleteUpload(r.Context(), principal, upload.RunID, object.Version, object.Digest, object.Size); err != nil {
		errorJSON(w, http.StatusConflict, "conflict")
		return
	}
	w.WriteHeader(http.StatusNoContent)
}
func (a *API) createUpload(w http.ResponseWriter, r *http.Request) {
	principal, ok := a.principal(w, r, "runs:write")
	if !ok {
		return
	}
	var body struct {
		BundleFormatVersion int    `json:"bundle_format_version"`
		BundleDigest        string `json:"bundle_digest"`
		BundleSize          int64  `json:"bundle_size"`
	}
	if decode(w, r, &body) != nil || body.BundleFormatVersion != 1 {
		errorJSON(w, http.StatusBadRequest, "invalid_request")
		return
	}
	upload, err := a.service.CreateUpload(r.Context(), principal, body.BundleDigest, body.BundleSize)
	if err != nil {
		errorJSON(w, http.StatusConflict, "conflict")
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"upload_id": upload.RunID, "run_id": upload.RunID, "required_digest": upload.Digest, "required_size": upload.Size, "upload_url": "/v1/local/uploads/" + upload.RunID, "expires_at": time.Now().UTC().Add(15 * time.Minute)})
}
func (a *API) principal(w http.ResponseWriter, r *http.Request, scope string) (controlplane.Principal, bool) {
	token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	if token == r.Header.Get("Authorization") {
		errorJSON(w, http.StatusUnauthorized, "unauthenticated")
		return controlplane.Principal{}, false
	}
	p, err := a.service.Authenticate(r.Context(), token, scope)
	if err != nil {
		if errors.Is(err, controlplane.ErrForbidden) {
			errorJSON(w, http.StatusForbidden, "forbidden")
			return controlplane.Principal{}, false
		}
		errorJSON(w, http.StatusUnauthorized, "unauthenticated")
		return controlplane.Principal{}, false
	}
	return p, true
}
func (a *API) listRuns(w http.ResponseWriter, r *http.Request) {
	p, ok := a.principal(w, r, "runs:read")
	if !ok {
		return
	}
	items, err := a.service.ListRuns(r.Context(), p, 100)
	if err != nil {
		errorJSON(w, 500, "internal")
		return
	}
	writeJSON(w, 200, map[string]any{"runs": items})
}
func (a *API) getRun(w http.ResponseWriter, r *http.Request) {
	p, ok := a.principal(w, r, "runs:read")
	if !ok {
		return
	}
	item, err := a.service.GetRun(r.Context(), p, r.PathValue("runID"))
	if err != nil {
		errorJSON(w, 404, "not_found")
		return
	}
	writeJSON(w, 200, item)
}
func (a *API) createJob(w http.ResponseWriter, r *http.Request) {
	p, ok := a.principal(w, r, "jobs:write")
	if !ok {
		return
	}
	var body controlplane.JobRequest
	if decode(w, r, &body) != nil {
		errorJSON(w, 400, "invalid_request")
		return
	}
	job, reused, err := a.service.CreateJob(r.Context(), p, r.Header.Get("Idempotency-Key"), body)
	if err != nil {
		code := "conflict"
		if err == controlplane.ErrIdempotencyConflict {
			code = "idempotency_conflict"
		}
		errorJSON(w, 409, code)
		return
	}
	status := http.StatusCreated
	if reused {
		status = http.StatusOK
	}
	writeJSON(w, status, map[string]any{"job_id": job.ID, "state": job.State})
}
func (a *API) getJob(w http.ResponseWriter, r *http.Request) {
	p, ok := a.principal(w, r, "jobs:read")
	if !ok {
		return
	}
	job, err := a.service.GetJob(r.Context(), p, r.PathValue("jobID"))
	if err != nil {
		errorJSON(w, 404, "not_found")
		return
	}
	writeJSON(w, 200, job)
}
func (a *API) cancelJob(w http.ResponseWriter, r *http.Request) {
	p, ok := a.principal(w, r, "jobs:write")
	if !ok {
		return
	}
	if err := a.service.RequestCancellation(r.Context(), p, r.PathValue("jobID")); err != nil {
		errorJSON(w, 404, "not_found")
		return
	}
	job, err := a.service.GetJob(r.Context(), p, r.PathValue("jobID"))
	if err != nil {
		errorJSON(w, 404, "not_found")
		return
	}
	writeJSON(w, 200, job)
}
func (a *API) events(w http.ResponseWriter, r *http.Request) {
	p, ok := a.principal(w, r, "jobs:read")
	if !ok {
		return
	}
	if _, err := a.service.GetJob(r.Context(), p, r.PathValue("jobID")); err != nil {
		errorJSON(w, 404, "not_found")
		return
	}
	flusher, ok := w.(http.Flusher)
	if !ok {
		errorJSON(w, 500, "internal")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	var sent int64
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		progress, err := a.service.CurrentProgress(r.Context(), p, r.PathValue("jobID"))
		if err == nil && progress.Sequence > sent {
			data, _ := json.Marshal(progress)
			_, _ = fmt.Fprintf(w, "id: %d\nevent: progress\ndata: %s\n\n", progress.Sequence, data)
			flusher.Flush()
			sent = progress.Sequence
		}
		select {
		case <-r.Context().Done():
			return
		case <-ticker.C:
		}
	}
}
func (a *API) download(w http.ResponseWriter, r *http.Request) {
	p, ok := a.principal(w, r, "artifacts:read")
	if !ok {
		return
	}
	artifact, err := a.service.GetArtifact(r.Context(), p, r.PathValue("artifactID"))
	if err != nil {
		errorJSON(w, 404, "not_found")
		return
	}
	file, err := a.artifacts.Open(artifact.Key, artifact.Version)
	if err != nil {
		errorJSON(w, 500, "internal")
		return
	}
	defer file.Close()
	w.Header().Set("Content-Type", artifact.MediaType)
	w.Header().Set("Content-Length", strconv.FormatInt(artifact.Size, 10))
	_, _ = io.Copy(w, file)
}
func (a *API) audit(w http.ResponseWriter, r *http.Request) {
	p, ok := a.principal(w, r, "audit:read")
	if !ok {
		return
	}
	items, err := a.service.ListAudit(r.Context(), p, 100)
	if err != nil {
		errorJSON(w, 500, "internal")
		return
	}
	writeJSON(w, 200, map[string]any{"records": items})
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
func errorJSON(w http.ResponseWriter, status int, code string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{"error": map[string]string{"code": code, "message": "request could not be completed", "request_id": requestID()}})
}

func requestID() string {
	var value [16]byte
	if _, err := rand.Read(value[:]); err != nil {
		return "00000000-0000-4000-8000-000000000000"
	}
	value[6] = value[6]&0x0f | 0x40
	value[8] = value[8]&0x3f | 0x80
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x", value[:4], value[4:6], value[6:8], value[8:10], value[10:])
}
