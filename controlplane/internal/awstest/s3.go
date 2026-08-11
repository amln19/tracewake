// Package awstest implements the small subset of the S3 and SQS wire
// protocols that the control plane depends on. It exists so adapter behaviour
// can be tested without an AWS account; it is never linked into the service.
package awstest

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/xml"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"time"
)

type storedObject struct {
	Body     []byte
	Checksum string
	Type     string
	Modified time.Time
}

// S3 is a versioned single-bucket object store.
type S3 struct {
	Server *httptest.Server
	Bucket string

	mutex          sync.Mutex
	objects        map[string]map[string]storedObject
	deleteFailures map[string]bool
	versions       int
}

func NewS3(bucket string) *S3 {
	fake := &S3{Bucket: bucket, objects: map[string]map[string]storedObject{}, deleteFailures: map[string]bool{}}
	fake.Server = httptest.NewServer(http.HandlerFunc(fake.serve))
	return fake
}

func (s *S3) Close() { s.Server.Close() }

// Tamper replaces stored bytes, which is what a client uploading content that
// does not match its declaration looks like to the control plane.
func (s *S3) Tamper(key, version string, body []byte) {
	s.mutex.Lock()
	defer s.mutex.Unlock()
	object := s.objects[key][version]
	object.Body = body
	s.objects[key][version] = object
}

func (s *S3) Age(key, version string, modified time.Time) {
	s.mutex.Lock()
	defer s.mutex.Unlock()
	object := s.objects[key][version]
	object.Modified = modified
	s.objects[key][version] = object
}

func (s *S3) FailDelete(key, version string) {
	s.mutex.Lock()
	defer s.mutex.Unlock()
	s.deleteFailures[key+"\x00"+version] = true
}

func (s *S3) Keys() []string {
	s.mutex.Lock()
	defer s.mutex.Unlock()
	var keys []string
	for key, versions := range s.objects {
		if len(versions) > 0 {
			keys = append(keys, key)
		}
	}
	return keys
}

func (s *S3) serve(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/")
	prefix := s.Bucket + "/"
	if path != s.Bucket && !strings.HasPrefix(path, prefix) {
		http.Error(w, "NoSuchBucket", http.StatusNotFound)
		return
	}
	key := strings.TrimPrefix(path, prefix)
	switch {
	case r.Method == http.MethodPut:
		s.put(w, r, key)
	case r.Method == http.MethodHead:
		s.head(w, r, key)
	case r.Method == http.MethodGet && r.URL.Query().Has("versions"):
		s.listVersions(w)
	case r.Method == http.MethodGet:
		s.get(w, r, key)
	case r.Method == http.MethodPost && r.URL.Query().Has("delete"):
		s.deleteObjects(w, r)
	default:
		http.Error(w, "MethodNotAllowed", http.StatusMethodNotAllowed)
	}
}

func (s *S3) put(w http.ResponseWriter, r *http.Request, key string) {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "IncompleteBody", http.StatusBadRequest)
		return
	}
	sum := sha256.Sum256(body)
	checksum := base64.StdEncoding.EncodeToString(sum[:])
	// S3 enforces and records a SHA-256 only when the client sends it as a
	// signed header. A presigned URL carries it in the query string, where the
	// service ignores it, so neither does this fake.
	if declared := r.Header.Get("x-amz-checksum-sha256"); declared != "" && declared != checksum {
		http.Error(w, "BadDigest", http.StatusBadRequest)
		return
	} else if declared == "" {
		checksum = ""
	}
	s.mutex.Lock()
	s.versions++
	version := fmt.Sprintf("v%d", s.versions)
	if s.objects[key] == nil {
		s.objects[key] = map[string]storedObject{}
	}
	s.objects[key][version] = storedObject{Body: body, Checksum: checksum, Type: r.Header.Get("Content-Type"), Modified: time.Now()}
	s.mutex.Unlock()
	w.Header().Set("x-amz-version-id", version)
	if checksum != "" {
		w.Header().Set("x-amz-checksum-sha256", checksum)
	}
	w.WriteHeader(http.StatusOK)
}

func (s *S3) lookup(key, version string) (storedObject, string, bool) {
	s.mutex.Lock()
	defer s.mutex.Unlock()
	versions := s.objects[key]
	if len(versions) == 0 {
		return storedObject{}, "", false
	}
	if version == "" {
		newest := ""
		for candidate := range versions {
			if newest == "" || len(candidate) > len(newest) || (len(candidate) == len(newest) && candidate > newest) {
				newest = candidate
			}
		}
		version = newest
	}
	object, ok := versions[version]
	return object, version, ok
}

func (s *S3) head(w http.ResponseWriter, r *http.Request, key string) {
	object, version, ok := s.lookup(key, r.URL.Query().Get("versionId"))
	if !ok {
		http.Error(w, "NoSuchKey", http.StatusNotFound)
		return
	}
	w.Header().Set("Content-Length", strconv.Itoa(len(object.Body)))
	w.Header().Set("x-amz-version-id", version)
	if object.Checksum != "" && (r.URL.Query().Get("x-amz-checksum-mode") != "" || r.Header.Get("x-amz-checksum-mode") != "") {
		w.Header().Set("x-amz-checksum-sha256", object.Checksum)
	}
	w.WriteHeader(http.StatusOK)
}

func (s *S3) get(w http.ResponseWriter, r *http.Request, key string) {
	object, version, ok := s.lookup(key, r.URL.Query().Get("versionId"))
	if !ok {
		http.Error(w, "NoSuchKey", http.StatusNotFound)
		return
	}
	if disposition := r.URL.Query().Get("response-content-disposition"); disposition != "" {
		w.Header().Set("Content-Disposition", disposition)
	}
	w.Header().Set("x-amz-version-id", version)
	w.Header().Set("Content-Length", strconv.Itoa(len(object.Body)))
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(object.Body)
}

type versionEntry struct {
	Key          string    `xml:"Key"`
	VersionID    string    `xml:"VersionId"`
	LastModified time.Time `xml:"LastModified"`
	Size         int64     `xml:"Size"`
	IsLatest     bool      `xml:"IsLatest"`
}

type listVersionsResult struct {
	XMLName     xml.Name       `xml:"ListVersionsResult"`
	IsTruncated bool           `xml:"IsTruncated"`
	Versions    []versionEntry `xml:"Version"`
}

func (s *S3) listVersions(w http.ResponseWriter) {
	s.mutex.Lock()
	result := listVersionsResult{}
	for key, versions := range s.objects {
		for version, object := range versions {
			result.Versions = append(result.Versions, versionEntry{
				Key: key, VersionID: version, LastModified: object.Modified.UTC(), Size: int64(len(object.Body)), IsLatest: true,
			})
		}
	}
	s.mutex.Unlock()
	w.Header().Set("Content-Type", "application/xml")
	_ = xml.NewEncoder(w).Encode(result)
}

type deleteRequest struct {
	XMLName xml.Name `xml:"Delete"`
	Objects []struct {
		Key       string `xml:"Key"`
		VersionID string `xml:"VersionId"`
	} `xml:"Object"`
}

func (s *S3) deleteObjects(w http.ResponseWriter, r *http.Request) {
	var request deleteRequest
	if err := xml.NewDecoder(r.Body).Decode(&request); err != nil {
		http.Error(w, "MalformedXML", http.StatusBadRequest)
		return
	}
	s.mutex.Lock()
	var failed []struct {
		Key       string `xml:"Key"`
		VersionID string `xml:"VersionId"`
		Code      string `xml:"Code"`
		Message   string `xml:"Message"`
	}
	for _, object := range request.Objects {
		if s.deleteFailures[object.Key+"\x00"+object.VersionID] {
			failed = append(failed, struct {
				Key       string `xml:"Key"`
				VersionID string `xml:"VersionId"`
				Code      string `xml:"Code"`
				Message   string `xml:"Message"`
			}{object.Key, object.VersionID, "AccessDenied", "denied for test"})
			continue
		}
		delete(s.objects[object.Key], object.VersionID)
	}
	s.mutex.Unlock()
	w.Header().Set("Content-Type", "application/xml")
	response := struct {
		XMLName xml.Name `xml:"DeleteResult"`
		Errors  any      `xml:"Error"`
	}{Errors: failed}
	_ = xml.NewEncoder(w).Encode(response)
}
