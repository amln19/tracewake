package workerapi

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/amln19/tracewake/controlplane/internal/artifacts"
	"github.com/amln19/tracewake/controlplane/internal/controlplane"
)

type openStore struct {
	artifacts.Store
	body []byte
}

func (s openStore) Open(context.Context, string, string) (io.ReadCloser, error) {
	return io.NopCloser(bytes.NewReader(s.body)), nil
}

func resultAPI(t *testing.T, artifactStore artifacts.Store) *API {
	t.Helper()
	raw, err := os.ReadFile("../../../contracts/schemas/v1/result-envelope.schema.json")
	if err != nil {
		t.Fatal(err)
	}
	api, err := New(nil, artifactStore, "", raw)
	if err != nil {
		t.Fatal(err)
	}
	return api
}

func TestResultEnvelopeValidationUsesTheVersionedContract(t *testing.T) {
	api := resultAPI(t, openStore{})
	accepted, err := filepath.Glob("../../../contracttest/fixtures/v1/accepted/result-envelope*.json")
	if err != nil {
		t.Fatal(err)
	}
	for _, path := range accepted {
		raw, readErr := os.ReadFile(path)
		if readErr != nil {
			t.Fatal(readErr)
		}
		if _, validateErr := api.validateResultEnvelope(raw); validateErr != nil {
			t.Errorf("accepted fixture %s: %v", filepath.Base(path), validateErr)
		}
	}
	for name, path := range map[string]string{
		"missing outcome":      "",
		"conflicting outcomes": "../../../contracttest/fixtures/v1/rejected/result-conflicting-outcomes.json",
		"missing artifact":     "../../../contracttest/fixtures/v1/rejected/result-otlp-without-artifact.json",
	} {
		var raw []byte
		if path == "" {
			raw = []byte(`{"protocol_version":1,"status":"succeeded"}`)
		} else {
			raw, err = os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
		}
		if _, validateErr := api.validateResultEnvelope(raw); validateErr == nil {
			t.Errorf("%s result was accepted", name)
		}
	}
}

func TestResultEnvelopeValidationRejectsNoncanonicalBytes(t *testing.T) {
	api := resultAPI(t, openStore{})
	raw, err := os.ReadFile("../../../contracttest/fixtures/v1/accepted/result-envelope.json")
	if err != nil {
		t.Fatal(err)
	}
	var document map[string]any
	if err := json.Unmarshal(raw, &document); err != nil {
		t.Fatal(err)
	}
	noncanonical, err := json.MarshalIndent(document, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := api.validateResultEnvelope(noncanonical); err == nil || !strings.Contains(err.Error(), "not canonical") {
		t.Fatalf("noncanonical result was not rejected: %v", err)
	}
}

func TestLocalizeResultBindingsMatchTheCommittedArtifacts(t *testing.T) {
	const (
		runID     = "run-1"
		objectKey = "workspaces/w/runs/run-1/bundle"
		version   = "version-1"
		digest    = "digest-1"
	)
	logicalDigest := "logical-1"
	cassetteFormat, eventSchema := 1, 3
	input := controlplane.ResultInput{
		RunID: runID, ObjectKey: objectKey, ObjectVersion: version,
		BundleDigest: digest, LogicalDigest: &logicalDigest, BundleFormat: 1,
		CassetteFormat: &cassetteFormat, EventSchema: &eventSchema,
	}
	companion := controlplane.CompanionArtifact{
		ArtifactID: "artifact-1", Kind: "localize_json",
		ObjectKey:     "workspaces/w/jobs/job-1/attempts/1/localize_json",
		ObjectVersion: "object-version-1", Digest: "artifact-digest-1",
		Size: 4096, MediaType: "application/json",
	}
	envelope := map[string]any{
		"status": "succeeded",
		"result": map[string]any{
			"kind": "localize",
			"artifact": map[string]any{
				"artifact_id":    companion.ArtifactID,
				"object_key":     companion.ObjectKey,
				"object_version": companion.ObjectVersion,
				"digest":         companion.Digest,
				"size":           json.Number("4096"),
				"media_type":     companion.MediaType,
				"schema_name":    nil,
				"schema_version": nil,
			},
			"provenance": map[string]any{
				"analysis_profile": "localize-v1",
				"inputs": []any{map[string]any{
					"run_id":                  input.RunID,
					"logical_run_digest":      logicalDigest,
					"bundle_digest":           input.BundleDigest,
					"bundle_object_key":       input.ObjectKey,
					"bundle_object_version":   input.ObjectVersion,
					"bundle_format_version":   json.Number("1"),
					"cassette_format_version": json.Number("1"),
					"event_schema_version":    json.Number("3"),
				}},
			},
		},
	}
	completion := controlplane.Completion{
		Kind:       "localize_result_json",
		Companions: []controlplane.CompanionArtifact{companion},
	}
	if err := validateResultBindings(envelope, completion, []controlplane.ResultInput{input}); err != nil {
		t.Fatalf("valid localize result was rejected: %v", err)
	}

	completion.Companions[0].Kind = "otlp_json"
	if err := validateResultBindings(envelope, completion, []controlplane.ResultInput{input}); err == nil {
		t.Fatal("localize result accepted a companion from another operation")
	}
}

func TestCanonicalResultJSONMatchesTheWorkerEncoding(t *testing.T) {
	raw, err := canonicalResultJSON(map[string]any{
		"largeFixed":   json.Number("1e15"),
		"negativeZero": json.Number("-0.0"),
		"scientific":   json.Number("1e16"),
		"text":         "é<&😀",
		"whole":        json.Number("1.0"),
	})
	if err != nil {
		t.Fatal(err)
	}
	want := "{\"largeFixed\":1000000000000000.0,\"negativeZero\":-0.0,\"scientific\":1e+16,\"text\":\"\\u00e9<&\\ud83d\\ude00\",\"whole\":1.0}\n"
	if string(raw) != want {
		t.Fatalf("canonical JSON=%q want=%q", raw, want)
	}
}

func TestWorkerAPIHashesResultObjectsAboveTheStoreVerificationLimit(t *testing.T) {
	body := bytes.Repeat([]byte("x"), int(artifacts.MaxVerifiedReadSize+1))
	api := resultAPI(t, openStore{body: body})
	object := artifacts.Object{
		Key:     "workspaces/w/jobs/j/attempts/1/otlp_json",
		Version: "version-1",
		Digest:  "0000000000000000000000000000000000000000000000000000000000000000",
		Size:    int64(len(body)),
	}
	if _, err := api.verifyStoredObject(context.Background(), object, false); err == nil {
		t.Fatal("stored bytes with a false digest were accepted")
	}
}
