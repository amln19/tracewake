package contracttest

import (
	"archive/tar"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"
	"unicode/utf8"
)

const (
	maxBundleBytes = 256 * 1024 * 1024
	maxBlobBytes   = 64 * 1024 * 1024
	maxEventBytes  = 64 * 1024 * 1024
	maxEvents      = 100_000
	maxEntries     = 10_000
)

var (
	digestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)
	uuidPattern   = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
	// W3C trace context version 00 is the only format this protocol carries.
	traceparentPattern = regexp.MustCompile(`^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$`)
)

type fixtureManifest struct {
	FixtureVersion int       `json:"fixture_version"`
	Fixtures       []fixture `json:"fixtures"`
}

type fixture struct {
	Path      string  `json:"path"`
	Validator string  `json:"validator"`
	Accepted  bool    `json:"accepted"`
	ErrorCode *string `json:"error_code"`
	SHA256    string  `json:"sha256"`
}

type failure struct {
	SchemaVersion int    `json:"schema_version"`
	Code          string `json:"code"`
	Message       string `json:"message"`
	Retryable     bool   `json:"retryable"`
}

type progress struct {
	ProtocolVersion int    `json:"protocol_version"`
	AttemptNumber   int    `json:"attempt_number"`
	Sequence        int64  `json:"sequence"`
	Stage           string `json:"stage"`
	Message         string `json:"message"`
}

type notification struct {
	ProtocolVersion int     `json:"protocol_version"`
	JobID           string  `json:"job_id"`
	JobVersion      int64   `json:"job_version"`
	Operation       string  `json:"operation"`
	Traceparent     *string `json:"traceparent"`
}

type claimRequest struct {
	ProtocolVersion int          `json:"protocol_version"`
	Notification    notification `json:"notification"`
	WorkerID        string       `json:"worker_id"`
}

type artifact struct {
	ArtifactID    string  `json:"artifact_id"`
	ObjectKey     string  `json:"object_key"`
	ObjectVersion string  `json:"object_version"`
	Digest        string  `json:"digest"`
	Size          int64   `json:"size"`
	MediaType     string  `json:"media_type"`
	SchemaName    *string `json:"schema_name"`
	SchemaVersion *int    `json:"schema_version"`
}

type claim struct {
	ProtocolVersion int        `json:"protocol_version"`
	JobID           string     `json:"job_id"`
	AttemptNumber   int        `json:"attempt_number"`
	AttemptToken    string     `json:"attempt_token"`
	LeaseExpiresAt  string     `json:"lease_expires_at"`
	InputArtifacts  []artifact `json:"input_artifacts"`
	Operation       string     `json:"operation"`
	Profile         *string    `json:"profile"`
}

type publicJobRequest struct {
	Operation string   `json:"operation"`
	RunIDs    []string `json:"run_ids"`
	Profile   *string  `json:"profile"`
}

type uploadDeclaration struct {
	BundleFormatVersion int    `json:"bundle_format_version"`
	BundleDigest        string `json:"bundle_digest"`
	BundleSize          int64  `json:"bundle_size"`
}

type resultEnvelope struct {
	ProtocolVersion int             `json:"protocol_version"`
	Status          string          `json:"status"`
	Result          json.RawMessage `json:"result"`
	Failure         json.RawMessage `json:"failure"`
}

type runProvenance struct {
	RunID                 string `json:"run_id"`
	LogicalRunDigest      string `json:"logical_run_digest"`
	BundleDigest          string `json:"bundle_digest"`
	BundleObjectKey       string `json:"bundle_object_key"`
	BundleObjectVersion   string `json:"bundle_object_version"`
	EventSchemaVersion    int    `json:"event_schema_version"`
	CassetteFormatVersion int    `json:"cassette_format_version"`
	BundleFormatVersion   int    `json:"bundle_format_version"`
}

type resultProvenance struct {
	Inputs          []runProvenance `json:"inputs"`
	AnalysisProfile string          `json:"analysis_profile"`
	LocusVersion    string          `json:"locus_version"`
	WorkerBuild     string          `json:"worker_build"`
	ProducedAt      string          `json:"produced_at"`
}

type validationResult struct {
	Kind             string           `json:"kind"`
	SchemaVersion    int              `json:"schema_version"`
	Valid            bool             `json:"valid"`
	RunID            string           `json:"run_id"`
	EventCount       int              `json:"event_count"`
	LogicalRunDigest string           `json:"logical_run_digest"`
	BundleDigest     string           `json:"bundle_digest"`
	Provenance       resultProvenance `json:"provenance"`
}

type artifactRef struct {
	ArtifactID    string  `json:"artifact_id"`
	ObjectKey     string  `json:"object_key"`
	ObjectVersion string  `json:"object_version"`
	Digest        string  `json:"digest"`
	Size          int64   `json:"size"`
	MediaType     string  `json:"media_type"`
	SchemaName    *string `json:"schema_name"`
	SchemaVersion *int    `json:"schema_version"`
}

type alignmentColumn struct {
	GoodIndex  *int     `json:"good_index"`
	BadIndex   *int     `json:"bad_index"`
	Similarity *float64 `json:"similarity"`
}

type diffResult struct {
	Kind          string            `json:"kind"`
	SchemaVersion int               `json:"schema_version"`
	Profile       string            `json:"profile"`
	Score         float64           `json:"score"`
	Divergence    *int              `json:"divergence"`
	GoodStepCount int               `json:"good_step_count"`
	BadStepCount  int               `json:"bad_step_count"`
	Alignment     []alignmentColumn `json:"alignment"`
	Provenance    resultProvenance  `json:"provenance"`
	HTML          artifactRef       `json:"html"`
}

type otlpResult struct {
	Kind          string           `json:"kind"`
	SchemaVersion int              `json:"schema_version"`
	SpanCount     int              `json:"span_count"`
	Provenance    resultProvenance `json:"provenance"`
	Artifact      artifactRef      `json:"artifact"`
}

type pprofResult struct {
	Kind          string           `json:"kind"`
	SchemaVersion int              `json:"schema_version"`
	SampleCount   int              `json:"sample_count"`
	Provenance    resultProvenance `json:"provenance"`
	Artifact      artifactRef      `json:"artifact"`
}

type bundleEntry struct {
	Path   string `json:"path"`
	Digest string `json:"digest"`
	Size   int64  `json:"size"`
}

type bundleManifest struct {
	BundleFormatVersion   int           `json:"bundle_format_version"`
	CassetteFormatVersion int           `json:"cassette_format_version"`
	EventSchemaVersion    int           `json:"event_schema_version"`
	RunID                 string        `json:"run_id"`
	EventCount            int           `json:"event_count"`
	LogicalRunDigest      string        `json:"logical_run_digest"`
	Events                bundleEntry   `json:"events"`
	Blobs                 []bundleEntry `json:"blobs"`
}

func decodeStrict(data []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		return fmt.Errorf("trailing JSON value")
	}
	return nil
}

func oneOf(value string, allowed ...string) bool {
	for _, candidate := range allowed {
		if value == candidate {
			return true
		}
	}
	return false
}

func validateFailure(data []byte) string {
	var value failure
	if decodeStrict(data, &value) != nil {
		return "invalid_message"
	}
	if value.SchemaVersion != 1 || len(value.Message) < 1 || len(value.Message) > 512 || !oneOf(
		value.Code,
		"invalid_bundle", "unsupported_version", "invalid_result", "unauthorized_input",
		"cancelled", "lease_lost", "artifact_commit_failed", "transient_dependency", "internal", "retry_exhausted",
	) {
		return "invalid_message"
	}
	return ""
}

func validateProgress(data []byte) string {
	var value progress
	if decodeStrict(data, &value) != nil {
		return "invalid_message"
	}
	if value.ProtocolVersion != 1 {
		return "unsupported_version"
	}
	if value.AttemptNumber < 1 || value.AttemptNumber > 3 || value.Sequence < 1 ||
		len(value.Message) < 1 || len(value.Message) > 512 || !oneOf(
		value.Stage, "claiming", "downloading", "validating", "analyzing", "uploading", "committing",
	) {
		return "invalid_message"
	}
	return ""
}

func validNotification(value notification) bool {
	if value.Traceparent != nil && !traceparentPattern.MatchString(*value.Traceparent) {
		return false
	}
	return value.ProtocolVersion == 1 && uuidPattern.MatchString(value.JobID) &&
		value.JobVersion >= 1 && oneOf(value.Operation, "validate", "diff", "otlp", "pprof")
}

func validateNotification(data []byte) string {
	var value notification
	if decodeStrict(data, &value) != nil || !validNotification(value) {
		return "invalid_message"
	}
	return ""
}

func validateClaimRequest(data []byte) string {
	var value claimRequest
	if decodeStrict(data, &value) != nil || value.ProtocolVersion != 1 ||
		!validNotification(value.Notification) || !uuidPattern.MatchString(value.WorkerID) {
		return "invalid_message"
	}
	return ""
}

func validArtifact(value artifact) bool {
	return uuidPattern.MatchString(value.ArtifactID) && len(value.ObjectKey) >= 1 &&
		len(value.ObjectKey) <= 512 && len(value.ObjectVersion) >= 1 &&
		len(value.ObjectVersion) <= 256 && digestPattern.MatchString(value.Digest) &&
		value.Size >= 0 && len(value.MediaType) >= 1 && len(value.MediaType) <= 128 &&
		((value.SchemaName == nil && value.SchemaVersion == nil) ||
			(value.SchemaName != nil && value.SchemaVersion != nil && *value.SchemaVersion >= 1))
}

func validateClaim(data []byte) string {
	var value claim
	if decodeStrict(data, &value) != nil || value.ProtocolVersion != 1 ||
		!uuidPattern.MatchString(value.JobID) || value.AttemptNumber < 1 || value.AttemptNumber > 3 ||
		len(value.AttemptToken) < 43 || len(value.AttemptToken) > 256 ||
		len(value.InputArtifacts) < 1 || len(value.InputArtifacts) > 2 ||
		!oneOf(value.Operation, "validate", "diff", "otlp", "pprof") {
		return "invalid_message"
	}
	for _, input := range value.InputArtifacts {
		if !validArtifact(input) {
			return "invalid_message"
		}
	}
	if value.Operation == "diff" && (value.Profile == nil || *value.Profile != "lexical-v1") {
		return "invalid_message"
	}
	return ""
}

func validatePublicJob(data []byte) string {
	var value publicJobRequest
	if decodeStrict(data, &value) != nil {
		return "invalid_request"
	}
	expected := 1
	if value.Operation == "diff" {
		expected = 2
	} else if !oneOf(value.Operation, "otlp", "pprof") {
		return "invalid_request"
	}
	if len(value.RunIDs) != expected {
		return "invalid_request"
	}
	seen := map[string]bool{}
	for _, id := range value.RunIDs {
		if !uuidPattern.MatchString(id) || seen[id] {
			return "invalid_request"
		}
		seen[id] = true
	}
	if value.Operation == "diff" && (value.Profile == nil || *value.Profile != "lexical-v1") {
		return "invalid_request"
	}
	if value.Operation != "diff" && value.Profile != nil {
		return "invalid_request"
	}
	return ""
}

func validateUpload(data []byte) string {
	var value uploadDeclaration
	if decodeStrict(data, &value) != nil {
		return "invalid_message"
	}
	if value.BundleFormatVersion != 1 {
		return "unsupported_version"
	}
	if !digestPattern.MatchString(value.BundleDigest) {
		return "invalid_digest"
	}
	if value.BundleSize < 0 || value.BundleSize > maxBundleBytes {
		return "invalid_message"
	}
	return ""
}

func validRunProvenance(value runProvenance) bool {
	return uuidPattern.MatchString(value.RunID) && digestPattern.MatchString(value.LogicalRunDigest) &&
		digestPattern.MatchString(value.BundleDigest) && len(value.BundleObjectKey) >= 1 &&
		len(value.BundleObjectKey) <= 512 && len(value.BundleObjectVersion) >= 1 &&
		value.EventSchemaVersion >= 1 && value.CassetteFormatVersion >= 1 &&
		value.BundleFormatVersion >= 1
}

func validProvenance(value resultProvenance, inputs int) bool {
	if len(value.Inputs) != inputs || len(value.AnalysisProfile) < 1 || len(value.AnalysisProfile) > 64 ||
		len(value.LocusVersion) < 1 || len(value.WorkerBuild) < 1 || len(value.ProducedAt) < 1 {
		return false
	}
	for _, input := range value.Inputs {
		if !validRunProvenance(input) {
			return false
		}
	}
	return true
}

func validArtifactRef(value artifactRef) bool {
	return uuidPattern.MatchString(value.ArtifactID) && len(value.ObjectKey) >= 1 && len(value.ObjectKey) <= 512 &&
		len(value.ObjectVersion) >= 1 && digestPattern.MatchString(value.Digest) && value.Size >= 0 &&
		len(value.MediaType) >= 1 && len(value.MediaType) <= 128
}

func validateValidationResult(data []byte) string {
	var result validationResult
	if decodeStrict(data, &result) != nil || result.SchemaVersion != 1 || !result.Valid ||
		!uuidPattern.MatchString(result.RunID) || result.EventCount < 0 || result.EventCount > maxEvents ||
		!digestPattern.MatchString(result.LogicalRunDigest) || !digestPattern.MatchString(result.BundleDigest) ||
		!validProvenance(result.Provenance, 1) {
		return "invalid_message"
	}
	return ""
}

func validateDiffResult(data []byte) string {
	var result diffResult
	if decodeStrict(data, &result) != nil || result.SchemaVersion != 1 || result.Profile != "lexical-v1" ||
		result.GoodStepCount < 0 || result.BadStepCount < 1 || !validProvenance(result.Provenance, 2) ||
		!validArtifactRef(result.HTML) {
		return "invalid_message"
	}
	if result.Divergence != nil && *result.Divergence < 1 {
		return "invalid_message"
	}
	for _, column := range result.Alignment {
		if column.GoodIndex == nil && column.BadIndex == nil {
			return "invalid_message"
		}
		if column.Similarity != nil && (*column.Similarity < 0 || *column.Similarity > 1) {
			return "invalid_message"
		}
	}
	return ""
}

func validateArtifactResult(data []byte, kind string) string {
	if kind == "otlp" {
		var result otlpResult
		if decodeStrict(data, &result) != nil || result.SchemaVersion != 1 || result.SpanCount < 1 ||
			!validProvenance(result.Provenance, 1) || !validArtifactRef(result.Artifact) {
			return "invalid_message"
		}
		return ""
	}
	var result pprofResult
	if decodeStrict(data, &result) != nil || result.SchemaVersion != 1 || result.SampleCount < 0 ||
		!validProvenance(result.Provenance, 1) || !validArtifactRef(result.Artifact) {
		return "invalid_message"
	}
	return ""
}

func validateResult(data []byte) string {
	var envelope resultEnvelope
	if decodeStrict(data, &envelope) != nil || envelope.ProtocolVersion != 1 {
		return "invalid_message"
	}
	if envelope.Status == "succeeded" {
		if len(envelope.Result) == 0 || string(envelope.Result) == "null" ||
			(len(envelope.Failure) != 0 && string(envelope.Failure) != "null") {
			return "invalid_message"
		}
		var kind struct {
			Kind string `json:"kind"`
		}
		if json.Unmarshal(envelope.Result, &kind) != nil {
			return "invalid_message"
		}
		switch kind.Kind {
		case "validation":
			return validateValidationResult(envelope.Result)
		case "diff":
			return validateDiffResult(envelope.Result)
		case "otlp", "pprof":
			return validateArtifactResult(envelope.Result, kind.Kind)
		default:
			return "invalid_message"
		}
	}
	if envelope.Status == "failed" {
		if len(envelope.Failure) == 0 || string(envelope.Failure) == "null" ||
			(len(envelope.Result) != 0 && string(envelope.Result) != "null") {
			return "invalid_message"
		}
		return validateFailure(envelope.Failure)
	}
	return "invalid_message"
}

func safeArchiveName(name string) bool {
	return name != "" && utf8.ValidString(name) && len([]byte(name)) <= 100 &&
		!strings.HasPrefix(name, "/") && !strings.Contains(name, `\`) &&
		path.Clean(name) == name && !strings.Contains(name, "../") && name != ".."
}

func digest(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func validateBundle(data []byte) string {
	if len(data) > maxBundleBytes || bytes.HasPrefix(data, []byte{0x1f, 0x8b}) ||
		bytes.HasPrefix(data, []byte("BZh")) || bytes.HasPrefix(data, []byte{0xfd, '7', 'z', 'X', 'Z'}) {
		return "invalid_archive"
	}
	reader := tar.NewReader(bytes.NewReader(data))
	entries := map[string][]byte{}
	names := []string{}
	total := int64(0)
	for {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil || len(names) >= maxEntries || !safeArchiveName(header.Name) ||
			header.Typeflag != tar.TypeReg || header.Mode != 0o644 || header.Uid != 0 ||
			header.Gid != 0 || header.ModTime.Unix() != 0 || header.Uname != "" ||
			header.Gname != "" || header.Linkname != "" || header.Format != tar.FormatUSTAR ||
			len(header.PAXRecords) != 0 || header.Size < 0 || header.Size > maxBundleBytes {
			return "invalid_archive"
		}
		if _, exists := entries[header.Name]; exists {
			return "invalid_archive"
		}
		content, err := io.ReadAll(io.LimitReader(reader, maxBundleBytes+1))
		if err != nil || int64(len(content)) != header.Size {
			return "invalid_archive"
		}
		total += int64(len(content))
		if total > maxBundleBytes {
			return "invalid_archive"
		}
		entries[header.Name] = content
		names = append(names, header.Name)
	}
	if !sort.StringsAreSorted(names) {
		return "invalid_archive"
	}
	manifestBytes, manifestOK := entries["manifest.json"]
	eventBytes, eventsOK := entries["events.jsonl"]
	if !manifestOK || !eventsOK || len(eventBytes) > maxEventBytes {
		return "invalid_archive"
	}
	var manifest bundleManifest
	if decodeStrict(manifestBytes, &manifest) != nil || manifest.BundleFormatVersion != 1 ||
		manifest.CassetteFormatVersion != 1 || manifest.EventSchemaVersion != 3 ||
		!uuidPattern.MatchString(manifest.RunID) || manifest.EventCount < 0 ||
		manifest.EventCount > maxEvents || !digestPattern.MatchString(manifest.LogicalRunDigest) ||
		manifest.Events.Path != "events.jsonl" || manifest.Events.Size != int64(len(eventBytes)) ||
		manifest.Events.Digest != digest(eventBytes) {
		return "invalid_archive"
	}
	if bytes.Count(eventBytes, []byte("\n")) != manifest.EventCount ||
		(len(eventBytes) > 0 && eventBytes[len(eventBytes)-1] != '\n') {
		return "invalid_archive"
	}
	expected := map[string]bool{"manifest.json": true, "events.jsonl": true}
	for _, blob := range manifest.Blobs {
		if !digestPattern.MatchString(blob.Digest) {
			return "invalid_archive"
		}
		wantPath := "blobs/" + blob.Digest[:2] + "/" + blob.Digest[2:4] + "/" + blob.Digest
		content, ok := entries[blob.Path]
		if blob.Path != wantPath || !ok ||
			blob.Size < 0 || blob.Size > maxBlobBytes || int64(len(content)) != blob.Size ||
			digest(content) != blob.Digest || expected[blob.Path] {
			return "invalid_archive"
		}
		expected[blob.Path] = true
	}
	if len(entries) != len(expected) {
		return "invalid_archive"
	}
	return ""
}

func validateFixture(data []byte, validator string) string {
	switch validator {
	case "bundle":
		return validateBundle(data)
	case "failure":
		return validateFailure(data)
	case "job-notification":
		return validateNotification(data)
	case "claim-request":
		return validateClaimRequest(data)
	case "claim":
		return validateClaim(data)
	case "progress":
		return validateProgress(data)
	case "public-job-request":
		return validatePublicJob(data)
	case "result-envelope":
		return validateResult(data)
	case "upload-declaration":
		return validateUpload(data)
	default:
		return "unknown_validator"
	}
}

func TestGoAgreesWithEverySharedFixture(t *testing.T) {
	root := filepath.Join("..", "fixtures", "v1")
	manifestBytes, err := os.ReadFile(filepath.Join(root, "manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	var manifest fixtureManifest
	if err := decodeStrict(manifestBytes, &manifest); err != nil {
		t.Fatal(err)
	}
	if manifest.FixtureVersion != 1 {
		t.Fatalf("fixture version = %d", manifest.FixtureVersion)
	}
	for _, fixture := range manifest.Fixtures {
		fixture := fixture
		t.Run(fixture.Path, func(t *testing.T) {
			data, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(fixture.Path)))
			if err != nil {
				t.Fatal(err)
			}
			if digest(data) != fixture.SHA256 {
				t.Fatalf("fixture digest changed")
			}
			code := validateFixture(data, fixture.Validator)
			if (code == "") != fixture.Accepted {
				t.Fatalf("accepted=%t, code=%q", fixture.Accepted, code)
			}
			if fixture.ErrorCode == nil {
				if code != "" {
					t.Fatalf("unexpected code %q", code)
				}
			} else if code != *fixture.ErrorCode {
				t.Fatalf("code=%q, want %q", code, *fixture.ErrorCode)
			}
		})
	}
}

func FuzzContractValidatorsDoNotPanic(f *testing.F) {
	f.Add([]byte(`{"protocol_version":1}`))
	f.Add([]byte{0x1f, 0x8b})
	f.Fuzz(func(t *testing.T, data []byte) {
		for _, validator := range []string{
			"bundle",
			"failure",
			"job-notification",
			"claim-request",
			"claim",
			"progress",
			"public-job-request",
			"result-envelope",
			"upload-declaration",
		} {
			validateFixture(data, validator)
		}
	})
}
