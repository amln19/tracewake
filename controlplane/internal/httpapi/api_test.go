package httpapi

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/amln19/tracewake/controlplane/internal/controlplane"
)

func TestProgressCursorAdvancesAcrossAttemptSequenceReset(t *testing.T) {
	if !progressIsNewer(controlplane.Progress{AttemptNumber: 2, Sequence: 1}, 1, 50) {
		t.Fatal("the first progress snapshot from a retry was suppressed")
	}
	if progressIsNewer(controlplane.Progress{AttemptNumber: 1, Sequence: 51}, 2, 1) {
		t.Fatal("a stale attempt advanced the progress cursor")
	}
}

func TestDashboardFallbackHasRestrictiveBrowserHeaders(t *testing.T) {
	directory := t.TempDir()
	index := `<!doctype html><title>Tracewake</title><main id="root"></main>`
	if err := os.WriteFile(filepath.Join(directory, "index.html"), []byte(index), 0o600); err != nil {
		t.Fatal(err)
	}
	handler := New(nil, nil, "", directory).Handler()
	request := httptest.NewRequest(http.MethodGet, "/jobs/not-script%3Cscript%3E", nil)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || response.Body.String() != index {
		t.Fatalf("dashboard response status=%d body=%q", response.Code, response.Body.String())
	}
	for name, want := range map[string]string{
		"X-Content-Type-Options":     "nosniff",
		"Referrer-Policy":            "no-referrer",
		"Cross-Origin-Opener-Policy": "same-origin",
	} {
		if got := response.Header().Get(name); got != want {
			t.Fatalf("%s=%q, want %q", name, got, want)
		}
	}
	csp := response.Header().Get("Content-Security-Policy")
	for _, directive := range []string{"script-src 'self'", "object-src 'none'", "base-uri 'none'", "frame-ancestors 'none'"} {
		if !strings.Contains(csp, directive) {
			t.Fatalf("CSP %q does not contain %q", csp, directive)
		}
	}
	if strings.Contains(csp, "amazonaws") {
		t.Fatalf("dashboard CSP exposes object storage: %q", csp)
	}
	if strings.Contains(response.Body.String(), "<script>") {
		t.Fatalf("route content was reflected into dashboard HTML: %q", response.Body.String())
	}
}

func TestListParametersValidateThePublicPageLimit(t *testing.T) {
	for _, test := range []struct {
		query      string
		wantCursor string
		wantLimit  int
		wantError  bool
	}{
		{query: "", wantLimit: 100},
		{query: "?cursor=opaque&limit=25", wantCursor: "opaque", wantLimit: 25},
		{query: "?limit=0", wantError: true},
		{query: "?limit=101", wantError: true},
		{query: "?limit=not-a-number", wantError: true},
	} {
		request := httptest.NewRequest(http.MethodGet, "/v1/runs"+test.query, nil)
		cursor, limit, err := listParameters(request)
		if (err != nil) != test.wantError {
			t.Fatalf("query=%q cursor=%q limit=%d err=%v", test.query, cursor, limit, err)
		}
		if err == nil && (cursor != test.wantCursor || limit != test.wantLimit) {
			t.Fatalf("query=%q cursor=%q limit=%d", test.query, cursor, limit)
		}
	}
}

func TestInlineReportPolicyRunsOnlyTheSelfContainedRenderer(t *testing.T) {
	for _, required := range []string{
		"default-src 'none'",
		"script-src 'unsafe-inline'",
		"form-action 'none'",
		"base-uri 'none'",
		"sandbox allow-scripts",
		"frame-ancestors 'self'",
	} {
		if !strings.Contains(reportContentSecurityPolicy, required) {
			t.Fatalf("report CSP %q does not contain %q", reportContentSecurityPolicy, required)
		}
	}
	for _, forbidden := range []string{"allow-same-origin", "connect-src", "allow-forms", "allow-top-navigation"} {
		if strings.Contains(reportContentSecurityPolicy, forbidden) {
			t.Fatalf("report CSP %q contains %q", reportContentSecurityPolicy, forbidden)
		}
	}
	if !inlineReport("inline", "diff_html", "text/html; charset=utf-8") {
		t.Fatal("the worker's report media type was not eligible for inline rendering")
	}
	for _, value := range [][3]string{
		{"attachment", "diff_html", "text/html; charset=utf-8"},
		{"inline", "worker_diagnostic", "text/html"},
		{"inline", "diff_html", "application/octet-stream"},
	} {
		if inlineReport(value[0], value[1], value[2]) {
			t.Fatalf("unsafe inline artifact accepted: %v", value)
		}
	}
}
