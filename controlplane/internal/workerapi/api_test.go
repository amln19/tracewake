package workerapi

import (
	"errors"
	"fmt"
	"net/http"
	"testing"

	"github.com/amln19/tracewake/controlplane/internal/controlplane"
)

func TestClaimFailurePreservesUncertainty(t *testing.T) {
	for _, test := range []struct {
		name       string
		err        error
		wantStatus int
		wantCode   string
	}{
		{name: "durable conflict", err: fmt.Errorf("claim: %w", controlplane.ErrConflict), wantStatus: http.StatusConflict, wantCode: "conflict"},
		{name: "unexpected failure", err: errors.New("database unavailable"), wantStatus: http.StatusServiceUnavailable, wantCode: "internal"},
	} {
		t.Run(test.name, func(t *testing.T) {
			status, code := claimFailure(test.err)
			if status != test.wantStatus || code != test.wantCode {
				t.Fatalf("status=%d code=%q, want status=%d code=%q", status, code, test.wantStatus, test.wantCode)
			}
		})
	}
}
