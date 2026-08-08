package httpapi

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

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
