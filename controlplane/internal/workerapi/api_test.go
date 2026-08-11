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

func TestWorkerFailuresDoNotMasqueradeAsCredentialOrLeaseLoss(t *testing.T) {
	for _, test := range []struct {
		name     string
		status   int
		code     string
		actual   error
		classify func(error) (int, string)
	}{
		{name: "invalid credential", actual: controlplane.ErrUnauthenticated, classify: workerAuthenticationFailure, status: http.StatusUnauthorized, code: "unauthenticated"},
		{name: "credential store unavailable", actual: errors.New("database unavailable"), classify: workerAuthenticationFailure, status: http.StatusServiceUnavailable, code: "internal"},
		{name: "stale attempt", actual: controlplane.ErrLeaseLost, classify: attemptFailure, status: http.StatusConflict, code: "lease_lost"},
		{name: "attempt store unavailable", actual: errors.New("database unavailable"), classify: attemptFailure, status: http.StatusServiceUnavailable, code: "internal"},
	} {
		t.Run(test.name, func(t *testing.T) {
			status, code := test.classify(test.actual)
			if status != test.status || code != test.code {
				t.Fatalf("status=%d code=%q, want status=%d code=%q", status, code, test.status, test.code)
			}
		})
	}
}
