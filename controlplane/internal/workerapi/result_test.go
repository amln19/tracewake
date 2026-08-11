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
		if validateErr := api.validateResultEnvelope(raw); validateErr != nil {
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
		if validateErr := api.validateResultEnvelope(raw); validateErr == nil {
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
	if err := api.validateResultEnvelope(noncanonical); err == nil || !strings.Contains(err.Error(), "not canonical") {
		t.Fatalf("noncanonical result was not rejected: %v", err)
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
