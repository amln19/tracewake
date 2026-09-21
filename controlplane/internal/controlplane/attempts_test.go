package controlplane

import (
	"context"
	"errors"
	"testing"
)

func TestWorkerPayloadValidationRejectsMalformedRequests(t *testing.T) {
	service := &Service{}
	for _, test := range []struct {
		name     string
		progress Progress
	}{
		{name: "missing message", progress: Progress{Sequence: 1, Stage: "analyzing"}},
		{name: "unknown stage", progress: Progress{Sequence: 1, Stage: "finishing", Message: "done"}},
	} {
		t.Run("progress/"+test.name, func(t *testing.T) {
			err := service.UpdateProgress(context.Background(), "", 1, "", test.progress)
			if !errors.Is(err, ErrInvalidRequest) {
				t.Fatalf("error=%v, want ErrInvalidRequest", err)
			}
		})
	}

	for _, test := range []struct {
		name      string
		code      string
		message   string
		retryable bool
	}{
		{name: "missing message", code: "internal", retryable: true},
		{name: "unknown code", code: "unknown", message: "bad", retryable: false},
		{name: "retryability mismatch", code: "internal", message: "bad", retryable: false},
	} {
		t.Run("failure/"+test.name, func(t *testing.T) {
			_, err := service.FailAttempt(context.Background(), "", 1, "", test.code, test.message, test.retryable)
			if !errors.Is(err, ErrInvalidRequest) {
				t.Fatalf("error=%v, want ErrInvalidRequest", err)
			}
		})
	}
}

func TestCompletionWireShapeMatchesTheWorkerProtocol(t *testing.T) {
	digest := "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
	base := Completion{
		ProtocolVersion: 1,
		AttemptNumber:   1,
		ArtifactID:      "018f7f28-df62-7bc4-9f45-6e6c32a19487",
		Kind:            "validation_json",
		ObjectKey:       "workspaces/w/jobs/j/attempts/1/validation_json",
		ObjectVersion:   "version-1",
		Digest:          digest,
		MediaType:       "application/json",
		SchemaName:      "result-envelope",
		Size:            123,
		SchemaVersion:   1,
		LogicalDigest:   digest,
		BundleDigest:    digest,
		EventCount:      1,
		BundleFormat:    1,
		CassetteFormat:  1,
		EventSchema:     3,
	}
	if !base.ValidWorkerRequest(1) {
		t.Fatal("valid validation completion was rejected")
	}

	analysis := base
	analysis.Kind = "otlp_result_json"
	analysis.LogicalDigest = ""
	analysis.BundleDigest = ""
	analysis.EventCount = 0
	analysis.BundleFormat = 0
	analysis.CassetteFormat = 0
	analysis.EventSchema = 0
	analysis.Companions = []CompanionArtifact{{
		ArtifactID:    "018f7f28-df62-7bc4-9f45-6e6c32a19489",
		Kind:          "otlp_json",
		ObjectKey:     "workspaces/w/jobs/j/attempts/1/otlp_json",
		ObjectVersion: "version-2",
		Digest:        digest,
		Size:          456,
		MediaType:     "application/json",
	}}
	if !analysis.ValidWorkerRequest(1) {
		t.Fatal("valid analysis completion was rejected")
	}

	for name, mutate := range map[string]func(*Completion){
		"wrong protocol":  func(value *Completion) { value.ProtocolVersion = 2 },
		"wrong attempt":   func(value *Completion) { value.AttemptNumber = 2 },
		"missing id":      func(value *Completion) { value.ArtifactID = "" },
		"wrong companion": func(value *Completion) { value.Companions[0].Kind = "pprof" },
		"run metadata":    func(value *Completion) { value.EventCount = 1 },
	} {
		t.Run(name, func(t *testing.T) {
			value := analysis
			value.Companions = append([]CompanionArtifact(nil), analysis.Companions...)
			mutate(&value)
			if value.ValidWorkerRequest(1) {
				t.Fatal("malformed completion was accepted")
			}
		})
	}
}
