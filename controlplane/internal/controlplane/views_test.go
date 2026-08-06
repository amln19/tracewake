package controlplane

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestFailureViewUsesVersionedPublicShape(t *testing.T) {
	code := "invalid_bundle"
	message := "bundle validation failed"
	encoded, err := json.Marshal(failureView(&code, &message))
	if err != nil {
		t.Fatal(err)
	}
	want := `{"schema_version":1,"code":"invalid_bundle","message":"bundle validation failed","retryable":false}`
	if string(encoded) != want {
		t.Fatalf("failure JSON = %s", encoded)
	}
}

func TestJobViewUsesPublicFailureAndProgressFields(t *testing.T) {
	view := JobView{
		Attempts:  []AttemptView{},
		Artifacts: []PublicArtifact{},
		Progress: &Progress{
			ProtocolVersion: 1,
			AttemptNumber:   2,
			Sequence:        3,
			Stage:           "analyzing",
			Message:         "working",
		},
	}
	encoded, err := json.Marshal(view)
	if err != nil {
		t.Fatal(err)
	}
	for _, field := range []string{`"failure":null`, `"attempts":[]`, `"artifacts":[]`, `"protocol_version":1`, `"attempt_number":2`} {
		if !strings.Contains(string(encoded), field) {
			t.Fatalf("job JSON %s does not contain %s", encoded, field)
		}
	}
	if strings.Contains(string(encoded), "failure_code") || strings.Contains(string(encoded), "failure_message") {
		t.Fatalf("job JSON exposes unversioned failure fields: %s", encoded)
	}
}
