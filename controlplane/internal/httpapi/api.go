package httpapi

import (
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"mime"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/amln19/locus/controlplane/internal/artifacts"
	"github.com/amln19/locus/controlplane/internal/controlplane"
)

type API struct {
	service   *controlplane.Service
	artifacts artifacts.Store
	baseURL   string
	dashboard string
}

func New(service *controlplane.Service, store artifacts.Store, baseURL string, dashboard ...string) *API {
	directory := ""
	if len(dashboard) > 0 {
		directory = dashboard[0]
	}
	return &API{service: service, artifacts: store, baseURL: baseURL, dashboard: directory}
}
func (a *API) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/browser/sessions", a.createBrowserSession)
	mux.HandleFunc("GET /v1/browser/session", a.refreshBrowserSession)
	mux.HandleFunc("DELETE /v1/browser/session", a.deleteBrowserSession)
	mux.HandleFunc("GET /v1/browser/artifacts/{artifactID}", a.browserArtifact)
	mux.HandleFunc("POST /v1/browser/runs/uploads", a.createBrowserUpload)
	mux.HandleFunc("PUT /v1/browser/runs/uploads/{runID}", a.putBrowserUpload)
	mux.HandleFunc("POST /v1/runs/uploads", a.createUpload)
	mux.HandleFunc("POST /v1/runs/uploads/{runID}/complete", a.completeUpload)
	mux.HandleFunc("GET /v1/runs", a.listRuns)
	mux.HandleFunc("GET /v1/runs/{runID}", a.getRun)
	mux.HandleFunc("POST /v1/jobs", a.createJob)
	mux.HandleFunc("GET /v1/jobs/{jobID}", a.getJob)
	mux.HandleFunc("POST /v1/jobs/{jobID}/cancel", a.cancelJob)
	mux.HandleFunc("GET /v1/jobs/{jobID}/events", a.events)
	mux.HandleFunc("GET /v1/artifacts/{artifactID}/download", a.download)
	mux.HandleFunc("GET /v1/audit", a.audit)
	if a.dashboard != "" {
		mux.Handle("/", a.dashboardHandler())
	}
	return securityHeaders(mux)
}

const browserCookie = "__Host-locus_session"

func (a *API) createBrowserSession(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Token string `json:"token"`
	}
	if decode(w, r, &body) != nil || body.Token == "" {
		errorJSON(w, http.StatusBadRequest, "invalid_request")
		return
	}
	session, err := a.service.ExchangeBrowserSession(r.Context(), body.Token)
	if err != nil {
		authError(w, err)
		return
	}
	a.setSessionCookie(w, session.Token, session.ExpiresAt)
	writeSession(w, http.StatusCreated, session)
}

func (a *API) refreshBrowserSession(w http.ResponseWriter, r *http.Request) {
	cookie, err := r.Cookie(browserCookie)
	if err != nil {
		errorJSON(w, http.StatusUnauthorized, "unauthenticated")
		return
	}
	session, err := a.service.RefreshBrowserSession(r.Context(), cookie.Value)
	if err != nil {
		authError(w, err)
		return
	}
	writeSession(w, http.StatusOK, session)
}

func (a *API) deleteBrowserSession(w http.ResponseWriter, r *http.Request) {
	cookie, err := r.Cookie(browserCookie)
	if err != nil {
		errorJSON(w, http.StatusUnauthorized, "unauthenticated")
		return
	}
	if err := a.service.RevokeBrowserSession(r.Context(), cookie.Value, r.Header.Get("X-Locus-CSRF")); err != nil {
		authError(w, err)
		return
	}
	a.setSessionCookie(w, "", time.Unix(1, 0))
	w.WriteHeader(http.StatusNoContent)
}

func writeSession(w http.ResponseWriter, status int, session controlplane.BrowserSession) {
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, status, map[string]any{
		"csrf_token": session.CSRFToken,
		"expires_at": session.ExpiresAt,
		"scopes":     session.Scopes,
	})
}

func (a *API) setSessionCookie(w http.ResponseWriter, token string, expires time.Time) {
	maxAge := int(time.Until(expires).Seconds())
	if token == "" {
		maxAge = -1
	}
	http.SetCookie(w, &http.Cookie{
		Name: browserCookie, Value: token, Path: "/", Expires: expires,
		MaxAge: maxAge, HttpOnly: true, Secure: true, SameSite: http.SameSiteStrictMode,
	})
}

func authError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, controlplane.ErrForbidden):
		errorJSON(w, http.StatusForbidden, "forbidden")
	case errors.Is(err, controlplane.ErrUnauthenticated):
		errorJSON(w, http.StatusUnauthorized, "unauthenticated")
	default:
		errorJSON(w, http.StatusInternalServerError, "internal")
	}
}

func (a *API) browserPrincipal(w http.ResponseWriter, r *http.Request, scope string) (controlplane.Principal, bool) {
	cookie, err := r.Cookie(browserCookie)
	if err != nil {
		errorJSON(w, http.StatusUnauthorized, "unauthenticated")
		return controlplane.Principal{}, false
	}
	p, _, err := a.service.AuthenticateBrowserSession(r.Context(), cookie.Value, scope, r.Header.Get("X-Locus-CSRF"), true)
	if err != nil {
		authError(w, err)
		return controlplane.Principal{}, false
	}
	return p, true
}

func (a *API) createBrowserUpload(w http.ResponseWriter, r *http.Request) {
	p, ok := a.browserPrincipal(w, r, "runs:write")
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
	upload, err := a.service.CreateUpload(r.Context(), p, body.BundleDigest, body.BundleSize)
	if err != nil {
		errorJSON(w, http.StatusConflict, "conflict")
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"run_id": upload.RunID, "state": "pending"})
}

func (a *API) putBrowserUpload(w http.ResponseWriter, r *http.Request) {
	p, ok := a.browserPrincipal(w, r, "runs:write")
	if !ok {
		return
	}
	upload, err := a.service.UploadFor(r.Context(), p, r.PathValue("runID"))
	if err != nil {
		errorJSON(w, http.StatusNotFound, "not_found")
		return
	}
	if upload.State != "pending" {
		errorJSON(w, http.StatusConflict, "conflict")
		return
	}
	if r.ContentLength != upload.Size || r.ContentLength < 0 || r.Header.Get("X-Locus-Bundle-Digest") != upload.Digest || r.Header.Get("X-Locus-Bundle-Format") != "1" {
		errorJSON(w, http.StatusConflict, "conflict")
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, upload.Size)
	object, err := a.artifacts.Put(r.Context(), upload.Key, upload.Digest, upload.Size, "application/x-tar", r.Body)
	if err != nil {
		errorJSON(w, http.StatusConflict, "conflict")
		return
	}
	if err := a.service.CompleteUpload(r.Context(), p, upload.RunID, object.Version, object.Digest, object.Size); err != nil {
		errorJSON(w, http.StatusConflict, "conflict")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"run_id": upload.RunID, "state": "validating"})
}
func (a *API) completeUpload(w http.ResponseWriter, r *http.Request) {
	principal, ok := a.principal(w, r, "runs:write")
	if !ok {
		return
	}
	var body struct {
		ObjectVersion string `json:"object_version"`
		Digest        string `json:"digest"`
		Size          int64  `json:"size"`
	}
	if decode(w, r, &body) != nil {
		errorJSON(w, http.StatusBadRequest, "invalid_request")
		return
	}
	upload, err := a.service.UploadFor(r.Context(), principal, r.PathValue("runID"))
	if err != nil {
		errorJSON(w, http.StatusNotFound, "not_found")
		return
	}
	if body.Digest != upload.Digest || body.Size != upload.Size {
		errorJSON(w, http.StatusConflict, "conflict")
		return
	}
	if upload.State != "pending" {
		if upload.Version == nil || *upload.Version != body.ObjectVersion {
			errorJSON(w, http.StatusConflict, "conflict")
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"run_id": upload.RunID, "state": upload.State})
		return
	}
	object, err := a.artifacts.Commit(r.Context(), upload.Key, body.ObjectVersion, upload.Digest, upload.Size)
	if err != nil {
		errorJSON(w, http.StatusConflict, "conflict")
		return
	}
	if err := a.service.CompleteUpload(r.Context(), principal, upload.RunID, object.Version, object.Digest, object.Size); err != nil {
		errorJSON(w, http.StatusConflict, "conflict")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"run_id": upload.RunID, "state": "validating"})
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
	grant, err := a.artifacts.PutGrant(r.Context(), upload.Key, upload.Digest, upload.Size, "application/x-tar")
	if err != nil {
		errorJSON(w, http.StatusConflict, "conflict")
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"upload_id":       upload.RunID,
		"run_id":          upload.RunID,
		"required_digest": upload.Digest,
		"required_size":   upload.Size,
		"upload_url":      artifacts.Absolute(a.baseURL, grant.URL),
		"upload_headers":  grant.Headers,
		"expires_at":      grant.ExpiresAt,
	})
}
func (a *API) principal(w http.ResponseWriter, r *http.Request, scope string) (controlplane.Principal, bool) {
	authorization := r.Header.Get("Authorization")
	var p controlplane.Principal
	var err error
	if authorization != "" {
		token := strings.TrimPrefix(authorization, "Bearer ")
		if token == authorization {
			errorJSON(w, http.StatusUnauthorized, "unauthenticated")
			return controlplane.Principal{}, false
		}
		p, err = a.service.Authenticate(r.Context(), token, scope)
	} else if cookie, cookieErr := r.Cookie(browserCookie); cookieErr == nil {
		requireCSRF := r.Method != http.MethodGet && r.Method != http.MethodHead && r.Method != http.MethodOptions
		p, _, err = a.service.AuthenticateBrowserSession(r.Context(), cookie.Value, scope, r.Header.Get("X-Locus-CSRF"), requireCSRF)
	} else {
		errorJSON(w, http.StatusUnauthorized, "unauthenticated")
		return controlplane.Principal{}, false
	}
	if err != nil {
		authError(w, err)
		return controlplane.Principal{}, false
	}
	return p, true
}

func (a *API) browserArtifact(w http.ResponseWriter, r *http.Request) {
	p, ok := a.principal(w, r, "artifacts:read")
	if !ok {
		return
	}
	artifact, err := a.service.GetArtifact(r.Context(), p, r.PathValue("artifactID"))
	if err != nil {
		errorJSON(w, http.StatusNotFound, "not_found")
		return
	}
	input, err := a.artifacts.Open(r.Context(), artifact.Key, artifact.Version)
	if err != nil {
		errorJSON(w, http.StatusInternalServerError, "internal")
		return
	}
	defer input.Close()
	inline := r.URL.Query().Get("disposition") == "inline" && artifact.Kind == "diff_html" && artifact.MediaType == "text/html"
	if inline {
		w.Header().Set("Content-Disposition", "inline")
		w.Header().Set("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox; frame-ancestors 'self'")
	} else {
		filename := artifact.Kind + extensionFor(artifact.MediaType)
		w.Header().Set("Content-Disposition", `attachment; filename="`+filename+`"`)
	}
	w.Header().Set("Cache-Control", "private, no-store")
	w.Header().Set("Content-Type", artifact.MediaType)
	w.Header().Set("Content-Length", strconv.FormatInt(artifact.Size, 10))
	w.Header().Set("ETag", `"`+artifact.Digest+`"`)
	w.WriteHeader(http.StatusOK)
	_, _ = io.Copy(w, io.LimitReader(input, artifact.Size))
}

func extensionFor(mediaType string) string {
	extensions, _ := mime.ExtensionsByType(mediaType)
	if len(extensions) > 0 {
		sort.Strings(extensions)
		return extensions[0]
	}
	return ".bin"
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
		switch {
		case errors.Is(err, controlplane.ErrUnsupported):
			errorJSON(w, http.StatusUnprocessableEntity, "unsupported_version")
		case errors.Is(err, controlplane.ErrInvalidRequest):
			errorJSON(w, http.StatusBadRequest, "invalid_request")
		case errors.Is(err, controlplane.ErrIdempotencyConflict):
			errorJSON(w, http.StatusConflict, "idempotency_conflict")
		default:
			errorJSON(w, http.StatusConflict, "conflict")
		}
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
	grant, err := a.artifacts.GetGrant(r.Context(), artifact.Key, artifact.Version, artifact.MediaType)
	if err != nil {
		errorJSON(w, 500, "internal")
		return
	}
	writeJSON(w, 200, map[string]any{
		"artifact_id":  artifact.ID,
		"download_url": artifacts.Absolute(a.baseURL, grant.URL),
		"expires_at":   grant.ExpiresAt,
		"digest":       artifact.Digest,
		"size":         artifact.Size,
		"media_type":   artifact.MediaType,
	})
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

func securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; frame-src 'self'; form-action 'self'")
		w.Header().Set("Cross-Origin-Opener-Policy", "same-origin")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		next.ServeHTTP(w, r)
	})
}

func (a *API) dashboardHandler() http.Handler {
	root := os.DirFS(a.dashboard)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			http.NotFound(w, r)
			return
		}
		name := strings.TrimPrefix(path.Clean("/"+r.URL.Path), "/")
		if name == "" {
			name = "index.html"
		}
		if !fs.ValidPath(name) {
			http.NotFound(w, r)
			return
		}
		file, err := root.Open(name)
		if err != nil {
			file, err = root.Open("index.html")
			name = "index.html"
		}
		if err != nil {
			http.NotFound(w, r)
			return
		}
		defer file.Close()
		info, err := file.Stat()
		if err != nil || info.IsDir() {
			http.NotFound(w, r)
			return
		}
		reader, ok := file.(io.ReadSeeker)
		if !ok {
			http.Error(w, "dashboard unavailable", http.StatusInternalServerError)
			return
		}
		if name == "index.html" {
			w.Header().Set("Cache-Control", "no-store")
		} else if strings.HasPrefix(name, "assets/") {
			w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
		}
		http.ServeContent(w, r, filepath.Base(name), info.ModTime(), reader)
	})
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
