package workerapi

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/amln19/tracewake/controlplane/internal/artifacts"
	"github.com/amln19/tracewake/controlplane/internal/controlplane"
	"github.com/amln19/tracewake/controlplane/internal/telemetry"
	"github.com/santhosh-tekuri/jsonschema/v6"
)

type API struct {
	service      *controlplane.Service
	artifacts    artifacts.Store
	baseURL      string
	metrics      *telemetry.Metrics
	resultSchema *jsonschema.Schema
}

func New(service *controlplane.Service, artifactStore artifacts.Store, baseURL string, schemaBytes []byte) (*API, error) {
	document, err := jsonschema.UnmarshalJSON(bytes.NewReader(schemaBytes))
	if err != nil {
		return nil, fmt.Errorf("decode result schema: %w", err)
	}
	compiler := jsonschema.NewCompiler()
	compiler.DefaultDraft(jsonschema.Draft2020)
	if err := compiler.AddResource("result-envelope.schema.json", document); err != nil {
		return nil, fmt.Errorf("load result schema: %w", err)
	}
	schema, err := compiler.Compile("result-envelope.schema.json")
	if err != nil {
		return nil, fmt.Errorf("compile result schema: %w", err)
	}
	return &API{service: service, artifacts: artifactStore, baseURL: baseURL, metrics: telemetry.NoMetrics(), resultSchema: schema}, nil
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
		status, code := workerAuthenticationFailure(err)
		writeError(w, status, code)
		return "", false
	}
	return id, true
}

func workerAuthenticationFailure(err error) (int, string) {
	if errors.Is(err, controlplane.ErrUnauthenticated) {
		return http.StatusUnauthorized, "unauthenticated"
	}
	return http.StatusServiceUnavailable, "internal"
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
		if !errors.Is(err, controlplane.ErrNotFound) {
			writeError(w, http.StatusServiceUnavailable, "internal")
			return
		}
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
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid_request")
		return
	}
	if err = a.service.AcknowledgeNotification(r.Context(), id); err != nil {
		writeError(w, http.StatusServiceUnavailable, "internal")
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
		status, code := claimFailure(err)
		writeError(w, status, code)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"protocol_version": 1, "job_id": claim.JobID, "attempt_number": claim.Attempt, "attempt_token": claim.AttemptToken, "lease_expires_at": claim.LeaseExpires, "operation": claim.Operation, "profile": claim.Profile, "input_artifacts": claim.Inputs})
}

func claimFailure(err error) (int, string) {
	if errors.Is(err, controlplane.ErrConflict) {
		return http.StatusConflict, "conflict"
	}
	return http.StatusServiceUnavailable, "internal"
}

func attemptFailure(err error) (int, string) {
	if errors.Is(err, controlplane.ErrLeaseLost) || errors.Is(err, controlplane.ErrConflict) || errors.Is(err, controlplane.ErrNotFound) {
		return http.StatusConflict, "lease_lost"
	}
	return http.StatusServiceUnavailable, "internal"
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
		status, code := attemptFailure(err)
		writeError(w, status, code)
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
		status, code := attemptFailure(err)
		writeError(w, status, code)
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
		status, code := attemptFailure(err)
		writeError(w, status, code)
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
		status, code := attemptFailure(err)
		writeError(w, status, code)
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
		status, code := attemptFailure(err)
		writeError(w, status, code)
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
	committed := []artifacts.Object{object}
	if err == nil {
		body.ObjectVersion = object.Version
		for index, companion := range body.Companions {
			companionObject, commitErr := a.artifacts.Commit(ctx, companion.ObjectKey, companion.ObjectVersion, companion.Digest, companion.Size)
			if commitErr != nil {
				err = commitErr
				break
			}
			body.Companions[index].ObjectVersion = companionObject.Version
			committed = append(committed, companionObject)
		}
	}
	if err == nil {
		var raw []byte
		var document map[string]any
		raw, err = a.verifyStoredObject(ctx, committed[0], true)
		if err == nil {
			document, err = a.validateResultEnvelope(raw)
		}
		for index := 1; err == nil && index < len(committed); index++ {
			_, err = a.verifyStoredObject(ctx, committed[index], false)
		}
		if err == nil {
			var inputs []controlplane.ResultInput
			inputs, err = a.service.ResultInputs(ctx, r.PathValue("job"), attempt, r.Header.Get("Tracewake-Attempt-Token"))
			if err == nil {
				err = validateResultBindings(document, body, inputs)
			}
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
		status, code := attemptFailure(err)
		writeError(w, status, code)
		return
	}
	writeJSON(w, 200, map[string]any{"protocol_version": 1, "status": "succeeded"})
}

func (a *API) verifyStoredObject(ctx context.Context, object artifacts.Object, capture bool) ([]byte, error) {
	reader, err := a.artifacts.Open(ctx, object.Key, object.Version)
	if err != nil {
		return nil, err
	}
	hash := sha256.New()
	var raw bytes.Buffer
	destination := io.Writer(hash)
	if capture {
		raw.Grow(int(object.Size))
		destination = io.MultiWriter(hash, &raw)
	}
	read, copyErr := io.Copy(destination, io.LimitReader(reader, object.Size+1))
	closeErr := reader.Close()
	if copyErr != nil {
		return nil, fmt.Errorf("hash stored result artifact: %w", copyErr)
	}
	if closeErr != nil {
		return nil, fmt.Errorf("close stored result artifact: %w", closeErr)
	}
	if read != object.Size || fmt.Sprintf("%x", hash.Sum(nil)) != object.Digest {
		return nil, errors.New("result artifact identity does not match stored bytes")
	}
	return raw.Bytes(), nil
}

func (a *API) validateResultEnvelope(raw []byte) (map[string]any, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var document any
	if err := decoder.Decode(&document); err != nil {
		return nil, fmt.Errorf("decode result envelope: %w", err)
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		return nil, errors.New("result envelope must contain one JSON value")
	}
	if err := a.resultSchema.Validate(document); err != nil {
		return nil, fmt.Errorf("validate result envelope: %w", err)
	}
	envelope, ok := document.(map[string]any)
	if !ok {
		return nil, errors.New("result envelope must be an object")
	}
	switch envelope["status"] {
	case "succeeded":
		if envelope["result"] == nil || envelope["failure"] != nil {
			return nil, errors.New("successful result envelope must contain only a result")
		}
	case "failed":
		if envelope["failure"] == nil || envelope["result"] != nil {
			return nil, errors.New("failed result envelope must contain only a failure")
		}
	default:
		return nil, errors.New("result envelope status is invalid")
	}
	canonical, err := canonicalResultJSON(document)
	if err != nil {
		return nil, err
	}
	if !bytes.Equal(raw, canonical) {
		return nil, errors.New("result envelope is not canonical JSON")
	}
	return envelope, nil
}

func validateResultBindings(envelope map[string]any, completion controlplane.Completion, inputs []controlplane.ResultInput) error {
	if envelope["status"] != "succeeded" {
		return errors.New("workers cannot commit failed result envelopes")
	}
	result, ok := envelope["result"].(map[string]any)
	if !ok {
		return errors.New("result is not an object")
	}
	kind, _ := result["kind"].(string)
	expectedArtifactKind := map[string]string{"validation": "validation_json", "diff": "diff_json", "otlp": "otlp_result_json", "pprof": "pprof_result_json"}[kind]
	expectedCompanionKind := map[string]string{"diff": "diff_html", "otlp": "otlp_json", "pprof": "pprof"}[kind]
	if expectedArtifactKind == "" || completion.Kind != expectedArtifactKind {
		return errors.New("result kind does not match the committed artifact kind")
	}
	if kind == "validation" {
		if len(inputs) != 1 || len(completion.Companions) != 0 || !validationResultMatches(result, completion, inputs[0]) {
			return errors.New("validation result does not match its input and completion metadata")
		}
	} else {
		if len(inputs) != expectedInputCount(kind) || len(completion.Companions) != 1 || completion.Companions[0].Kind != expectedCompanionKind {
			return errors.New("analysis result has an invalid input or companion set")
		}
		artifactField := "artifact"
		if kind == "diff" {
			artifactField = "html"
		}
		artifact, ok := result[artifactField].(map[string]any)
		if !ok || !artifactReferenceMatches(artifact, completion.Companions[0]) {
			return errors.New("result artifact reference does not match the committed companion")
		}
	}
	provenance, ok := result["provenance"].(map[string]any)
	if !ok || !provenanceInputsMatch(provenance, completion, inputs, kind == "validation") {
		return errors.New("result provenance does not match the authoritative job inputs")
	}
	return nil
}

func expectedInputCount(kind string) int {
	if kind == "diff" {
		return 2
	}
	return 1
}

func validationResultMatches(result map[string]any, completion controlplane.Completion, input controlplane.ResultInput) bool {
	return stringValue(result["run_id"]) == input.RunID &&
		stringValue(result["bundle_digest"]) == input.BundleDigest &&
		stringValue(result["logical_run_digest"]) == completion.LogicalDigest &&
		integerValue(result["event_count"]) == int64(completion.EventCount) &&
		completion.BundleDigest == input.BundleDigest && completion.BundleFormat == input.BundleFormat
}

func provenanceInputsMatch(provenance map[string]any, completion controlplane.Completion, expected []controlplane.ResultInput, validating bool) bool {
	values, ok := provenance["inputs"].([]any)
	if !ok || len(values) != len(expected) {
		return false
	}
	for index, expectedInput := range expected {
		value, ok := values[index].(map[string]any)
		if !ok {
			return false
		}
		logicalDigest := pointerString(expectedInput.LogicalDigest)
		cassetteFormat := pointerInt(expectedInput.CassetteFormat)
		eventSchema := pointerInt(expectedInput.EventSchema)
		if validating {
			logicalDigest = completion.LogicalDigest
			cassetteFormat = int64(completion.CassetteFormat)
			eventSchema = int64(completion.EventSchema)
		}
		if stringValue(value["run_id"]) != expectedInput.RunID ||
			stringValue(value["logical_run_digest"]) != logicalDigest ||
			stringValue(value["bundle_digest"]) != expectedInput.BundleDigest ||
			stringValue(value["bundle_object_key"]) != expectedInput.ObjectKey ||
			stringValue(value["bundle_object_version"]) != expectedInput.ObjectVersion ||
			integerValue(value["bundle_format_version"]) != int64(expectedInput.BundleFormat) ||
			integerValue(value["cassette_format_version"]) != cassetteFormat ||
			integerValue(value["event_schema_version"]) != eventSchema {
			return false
		}
	}
	return true
}

func artifactReferenceMatches(value map[string]any, companion controlplane.CompanionArtifact) bool {
	return stringValue(value["artifact_id"]) == companion.ArtifactID &&
		stringValue(value["object_key"]) == companion.ObjectKey &&
		stringValue(value["object_version"]) == companion.ObjectVersion &&
		stringValue(value["digest"]) == companion.Digest &&
		integerValue(value["size"]) == companion.Size &&
		stringValue(value["media_type"]) == companion.MediaType &&
		nullableStringMatches(value["schema_name"], companion.SchemaName) &&
		nullableIntMatches(value["schema_version"], companion.SchemaVersion)
}

func stringValue(value any) string {
	text, _ := value.(string)
	return text
}

func integerValue(value any) int64 {
	number, ok := value.(json.Number)
	if !ok {
		return -1
	}
	integer, err := number.Int64()
	if err != nil {
		return -1
	}
	return integer
}

func pointerString(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func pointerInt(value *int) int64 {
	if value == nil {
		return -1
	}
	return int64(*value)
}

func nullableStringMatches(value any, expected *string) bool {
	if expected == nil {
		return value == nil
	}
	return stringValue(value) == *expected
}

func nullableIntMatches(value any, expected *int) bool {
	if expected == nil {
		return value == nil
	}
	return integerValue(value) == int64(*expected)
}

func canonicalResultJSON(value any) ([]byte, error) {
	var result bytes.Buffer
	if err := appendCanonicalJSON(&result, value); err != nil {
		return nil, err
	}
	result.WriteByte('\n')
	return result.Bytes(), nil
}

func appendCanonicalJSON(destination *bytes.Buffer, value any) error {
	switch value := value.(type) {
	case nil:
		destination.WriteString("null")
	case bool:
		destination.WriteString(strconv.FormatBool(value))
	case string:
		appendPythonJSONString(destination, value)
	case json.Number:
		destination.WriteString(canonicalPythonNumber(value))
	case []any:
		destination.WriteByte('[')
		for index, item := range value {
			if index > 0 {
				destination.WriteByte(',')
			}
			if err := appendCanonicalJSON(destination, item); err != nil {
				return err
			}
		}
		destination.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(value))
		for key := range value {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		destination.WriteByte('{')
		for index, key := range keys {
			if index > 0 {
				destination.WriteByte(',')
			}
			appendPythonJSONString(destination, key)
			destination.WriteByte(':')
			if err := appendCanonicalJSON(destination, value[key]); err != nil {
				return err
			}
		}
		destination.WriteByte('}')
	default:
		return fmt.Errorf("result envelope contains unsupported JSON value %T", value)
	}
	return nil
}

func canonicalPythonNumber(number json.Number) string {
	raw := number.String()
	if !strings.ContainsAny(raw, ".eE") {
		return raw
	}
	value, err := strconv.ParseFloat(raw, 64)
	if err != nil {
		return raw
	}
	formatted := strconv.FormatFloat(value, 'g', -1, 64)
	if separator := strings.IndexByte(formatted, 'e'); separator >= 0 {
		exponent, exponentErr := strconv.Atoi(formatted[separator+1:])
		if exponentErr == nil && exponent >= -4 && exponent < 16 {
			formatted = strconv.FormatFloat(value, 'f', -1, 64)
		}
	}
	if !strings.ContainsAny(formatted, ".eE") {
		formatted += ".0"
	}
	return formatted
}

func appendPythonJSONString(destination *bytes.Buffer, value string) {
	destination.WriteByte('"')
	for len(value) > 0 {
		runeValue, size := utf8.DecodeRuneInString(value)
		value = value[size:]
		switch runeValue {
		case '"', '\\':
			destination.WriteByte('\\')
			destination.WriteRune(runeValue)
		case '\b':
			destination.WriteString(`\b`)
		case '\f':
			destination.WriteString(`\f`)
		case '\n':
			destination.WriteString(`\n`)
		case '\r':
			destination.WriteString(`\r`)
		case '\t':
			destination.WriteString(`\t`)
		default:
			switch {
			case runeValue < 0x20:
				fmt.Fprintf(destination, `\u%04x`, runeValue)
			case runeValue <= 0x7f:
				destination.WriteRune(runeValue)
			case runeValue <= 0xffff:
				fmt.Fprintf(destination, `\u%04x`, runeValue)
			default:
				value := runeValue - 0x10000
				fmt.Fprintf(destination, `\u%04x\u%04x`, 0xd800+(value>>10), 0xdc00+(value&0x3ff))
			}
		}
	}
	destination.WriteByte('"')
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
		status, code := attemptFailure(err)
		writeError(w, status, code)
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
