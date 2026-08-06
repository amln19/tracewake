package httpapi

import (
	"encoding/json"
	"net/http"
	"strings"

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
	if json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&body) != nil || body.BundleFormatVersion != 1 {
		errorJSON(w, http.StatusBadRequest, "invalid_request")
		return
	}
	upload, err := a.service.CreateUpload(r.Context(), principal, body.BundleDigest, body.BundleSize)
	if err != nil {
		errorJSON(w, http.StatusConflict, "conflict")
		return
	}
	json.NewEncoder(w).Encode(map[string]any{"run_id": upload.RunID, "required_digest": upload.Digest, "required_size": upload.Size, "upload_url": "/v1/local/uploads/" + upload.RunID})
}
func (a *API) principal(w http.ResponseWriter, r *http.Request, scope string) (controlplane.Principal, bool) {
	token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	if token == r.Header.Get("Authorization") {
		errorJSON(w, http.StatusUnauthorized, "unauthenticated")
		return controlplane.Principal{}, false
	}
	p, err := a.service.Authenticate(r.Context(), token, scope)
	if err != nil {
		errorJSON(w, http.StatusUnauthorized, "unauthenticated")
		return controlplane.Principal{}, false
	}
	return p, true
}
func errorJSON(w http.ResponseWriter, status int, code string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{"error": map[string]string{"code": code, "message": "request could not be completed"}})
}
