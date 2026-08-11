package controlplane

import (
	"errors"
	"testing"
)

func TestNormalizedDigestRejectsInvalidInputShapes(t *testing.T) {
	profile := "lexical-v1"
	valid := JobRequest{Operation: "diff", RunIDs: []string{"00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"}, Profile: &profile}
	if _, err := normalizedDigest(valid); err != nil {
		t.Fatalf("valid diff rejected: %v", err)
	}
	for _, request := range []JobRequest{
		{Operation: "diff", RunIDs: []string{"a", "a"}, Profile: &profile},
		{Operation: "diff", RunIDs: []string{"a", "b"}},
		{Operation: "otlp", RunIDs: []string{"a"}, Profile: &profile},
		{Operation: "unknown", RunIDs: []string{"a"}},
	} {
		if _, err := normalizedDigest(request); err == nil {
			t.Fatalf("invalid request was accepted: %#v", request)
		}
	}
}

func TestNormalizedDigestDistinguishesOrderedInputs(t *testing.T) {
	profile := "lexical-v1"
	first := "00000000-0000-4000-8000-000000000001"
	second := "00000000-0000-4000-8000-000000000002"
	left, err := normalizedDigest(JobRequest{Operation: "diff", RunIDs: []string{first, second}, Profile: &profile})
	if err != nil {
		t.Fatal(err)
	}
	right, err := normalizedDigest(JobRequest{Operation: "diff", RunIDs: []string{second, first}, Profile: &profile})
	if err != nil {
		t.Fatal(err)
	}
	if left == right {
		t.Fatal("ordered diff inputs must have distinct identities")
	}
}

func TestCreateJobRejectsInvalidTransportBeforeDatabaseAccess(t *testing.T) {
	service := Service{}
	profile := "lexical-v1"
	validRequest := JobRequest{
		Operation: "diff",
		RunIDs: []string{
			"00000000-0000-4000-8000-000000000001",
			"00000000-0000-4000-8000-000000000002",
		},
		Profile: &profile,
	}
	for _, test := range []struct {
		name    string
		key     string
		request JobRequest
	}{
		{name: "unicode key", key: "café", request: validRequest},
		{name: "space in key", key: "not visible", request: validRequest},
		{name: "oversized key", key: string(make([]byte, 256)), request: validRequest},
		{name: "malformed run ID", key: "valid-key", request: JobRequest{Operation: "otlp", RunIDs: []string{"not-a-uuid"}}},
	} {
		t.Run(test.name, func(t *testing.T) {
			if _, _, err := service.CreateJob(t.Context(), Principal{}, test.key, test.request); !errors.Is(err, ErrInvalidRequest) {
				t.Fatalf("error=%v, want invalid request", err)
			}
		})
	}
}

func TestCreateUploadRejectsMalformedDeclarationBeforeDatabaseAccess(t *testing.T) {
	service := Service{}
	for _, digest := range []string{"not-a-digest", "ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ"} {
		if _, err := service.CreateUpload(t.Context(), Principal{}, digest, 1); !errors.Is(err, ErrInvalidRequest) {
			t.Fatalf("digest=%q error=%v, want invalid request", digest, err)
		}
	}
}
