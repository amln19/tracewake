package artifacts

import (
	"crypto/hmac"
	"encoding/json"
	"io"
	"net/http"
	"strconv"
	"time"
)

// Handler serves the signed object URLs produced by a Filesystem store. A
// caller proves authorization with the signature alone, so the handler never
// consults the database and never reveals whether an unsigned key exists.
func (s *Filesystem) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("PUT /objects/{key...}", s.serveUpload)
	mux.HandleFunc("GET /objects/{key...}", s.serveDownload)
	return mux
}

func (s *Filesystem) serveUpload(w http.ResponseWriter, r *http.Request) {
	key := r.PathValue("key")
	digest := r.URL.Query().Get("digest")
	size, err := strconv.ParseInt(r.URL.Query().Get("size"), 10, 64)
	if err != nil || !s.authorized(r, "PUT", key, "", digest, size) {
		http.Error(w, "object grant is not valid", http.StatusForbidden)
		return
	}
	object, err := s.put(key, digest, size, http.MaxBytesReader(w, r.Body, size+1))
	if err != nil {
		http.Error(w, "object does not match its grant", http.StatusBadRequest)
		return
	}
	w.Header().Set("Locus-Object-Version", object.Version)
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	_ = json.NewEncoder(w).Encode(object)
}

func (s *Filesystem) serveDownload(w http.ResponseWriter, r *http.Request) {
	key := r.PathValue("key")
	version := r.URL.Query().Get("version")
	if !s.authorized(r, "GET", key, version, "", 0) {
		http.Error(w, "object grant is not valid", http.StatusForbidden)
		return
	}
	file, err := s.open(key)
	if err != nil {
		http.Error(w, "object is not available", http.StatusNotFound)
		return
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		http.Error(w, "object is not available", http.StatusNotFound)
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Content-Disposition", "attachment")
	w.Header().Set("Content-Length", strconv.FormatInt(info.Size(), 10))
	w.Header().Set("Locus-Object-Version", version)
	_, _ = io.Copy(w, file)
}

func (s *Filesystem) authorized(r *http.Request, method, key, version, digest string, size int64) bool {
	if !safeKey(key) {
		return false
	}
	expires, err := strconv.ParseInt(r.URL.Query().Get("expires"), 10, 64)
	if err != nil || s.now().After(time.Unix(expires, 0)) {
		return false
	}
	expected := s.sign(method, key, version, digest, size, expires)
	return hmac.Equal([]byte(expected), []byte(r.URL.Query().Get("signature")))
}
