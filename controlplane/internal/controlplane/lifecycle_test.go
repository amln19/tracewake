package controlplane

import "testing"

func TestNormalizedDigestRejectsInvalidInputShapes(t *testing.T) {
	profile := "lexical-v1"
	valid := JobRequest{Operation: "diff", RunIDs: []string{"a", "b"}, Profile: &profile}
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
	left, err := normalizedDigest(JobRequest{Operation: "diff", RunIDs: []string{"a", "b"}, Profile: &profile})
	if err != nil {
		t.Fatal(err)
	}
	right, err := normalizedDigest(JobRequest{Operation: "diff", RunIDs: []string{"b", "a"}, Profile: &profile})
	if err != nil {
		t.Fatal(err)
	}
	if left == right {
		t.Fatal("ordered diff inputs must have distinct identities")
	}
}
