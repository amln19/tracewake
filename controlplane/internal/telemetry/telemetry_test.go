package telemetry

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel/trace"
)

func lines(t *testing.T, buffer *bytes.Buffer) []map[string]any {
	t.Helper()
	var records []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(buffer.String()), "\n") {
		if line == "" {
			continue
		}
		var record map[string]any
		if err := json.Unmarshal([]byte(line), &record); err != nil {
			t.Fatalf("telemetry line is not JSON: %v: %s", err, line)
		}
		records = append(records, record)
	}
	return records
}

func provider(t *testing.T) (*Provider, *bytes.Buffer) {
	t.Helper()
	buffer := &bytes.Buffer{}
	instance, err := Start(context.Background(), Options{
		ServiceName: "locus-test", ServiceVersion: "0.0.0", Environment: "test",
		Namespace: "Locus/Test", Writer: buffer, MetricInterval: time.Hour, Synchronous: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = instance.Shutdown(context.Background()) })
	return instance, buffer
}

func TestSpansCarryParentAndResourceIdentity(t *testing.T) {
	instance, buffer := provider(t)
	ctx, parent := Span(context.Background(), "job.create", trace.SpanKindServer)
	_, child := Span(ctx, "outbox.publish", trace.SpanKindProducer)
	child.End()
	parent.End()
	if err := instance.Flush(context.Background()); err != nil {
		t.Fatal(err)
	}
	records := lines(t, buffer)
	if len(records) != 2 {
		t.Fatalf("expected two spans, got %d", len(records))
	}
	first, second := records[0], records[1]
	if first["name"] != "outbox.publish" || second["name"] != "job.create" {
		t.Fatalf("unexpected span order: %v", records)
	}
	if first["parent_span_id"] != second["span_id"] {
		t.Fatalf("child is not linked to its parent: %v", records)
	}
	if first["trace_id"] != second["trace_id"] {
		t.Fatalf("spans landed in different traces: %v", records)
	}
	if first["service_name"] != "locus-test" || first["deployment_environment"] != "test" {
		t.Fatalf("span lost its resource identity: %v", first)
	}
	if first["kind"] != "producer" || second["kind"] != "server" {
		t.Fatalf("span kinds were not preserved: %v", records)
	}
}

func TestNotificationTraceContextCrossesTheQueue(t *testing.T) {
	instance, buffer := provider(t)
	ctx, producer := Span(context.Background(), "job.create", trace.SpanKindServer)
	carrier := Traceparent(ctx)
	producer.End()

	consumed := Continue(context.Background(), carrier)
	_, consumer := Span(consumed, "job.claim", trace.SpanKindConsumer)
	consumer.End()
	if err := instance.Flush(context.Background()); err != nil {
		t.Fatal(err)
	}
	records := lines(t, buffer)
	if len(records) != 2 || records[0]["trace_id"] != records[1]["trace_id"] {
		t.Fatalf("a claim resumed from a notification left the trace: %v", records)
	}
	if records[1]["parent_span_id"] != records[0]["span_id"] {
		t.Fatalf("the claim is not a child of the producer: %v", records)
	}
}

func TestMetricsExportEmbeddedFormat(t *testing.T) {
	instance, buffer := provider(t)
	ctx := context.Background()
	instance.Metrics().JobCreated(ctx, "diff")
	instance.Metrics().JobTerminal(ctx, "diff", "succeeded", 1500*time.Millisecond)
	if err := instance.Flush(ctx); err != nil {
		t.Fatal(err)
	}
	byMetric := map[string]map[string]any{}
	for _, record := range lines(t, buffer) {
		metadata := record["_aws"].(map[string]any)
		directives := metadata["CloudWatchMetrics"].([]any)
		for _, directive := range directives {
			details := directive.(map[string]any)
			if details["Namespace"] != "Locus/Test" {
				t.Fatalf("metric landed in namespace %v", details["Namespace"])
			}
			for _, definition := range details["Metrics"].([]any) {
				byMetric[definition.(map[string]any)["Name"].(string)] = record
			}
		}
	}
	created, ok := byMetric["JobsCreated"]
	if !ok {
		t.Fatalf("JobsCreated was not exported: %s", buffer.String())
	}
	if created["Operation"] != "diff" || created["JobsCreated"] != float64(1) {
		t.Fatalf("unexpected counter record: %v", created)
	}
	duration, ok := byMetric["JobDurationMillis"]
	if !ok {
		t.Fatalf("JobDurationMillis was not exported: %s", buffer.String())
	}
	statistics := duration["JobDurationMillis"].(map[string]any)
	if statistics["Count"] != float64(1) || statistics["Sum"] != float64(1500) {
		t.Fatalf("unexpected histogram record: %v", statistics)
	}
	if duration["Outcome"] != "succeeded" {
		t.Fatalf("histogram lost its outcome dimension: %v", duration)
	}
}

func TestUnknownDimensionValuesCollapse(t *testing.T) {
	instance, buffer := provider(t)
	ctx := context.Background()
	instance.Metrics().JobCreated(ctx, "a-profile-a-client-invented")
	instance.Metrics().AttemptFenced(ctx, "something-new")
	if err := instance.Flush(ctx); err != nil {
		t.Fatal(err)
	}
	for _, record := range lines(t, buffer) {
		for _, dimension := range []string{"Operation", "Reason"} {
			if value, present := record[dimension]; present && value != otherValue {
				t.Fatalf("unbounded dimension value %q survived", value)
			}
		}
	}
}

// The bill and the alarm evaluation both scale with the number of series, so
// the complete instrument set has to stay small enough to enumerate.
func TestSeriesCountIsBounded(t *testing.T) {
	size := func(values []string) int { return len(values) + 1 }
	total := size(Surfaces)*size(Routes)*len(StatusClasses) + // HttpRequests
		size(Surfaces)*size(Routes) + // HttpDurationMillis
		size(Operations) + // JobsCreated
		2*size(Operations)*size(Outcomes) + // JobsTerminal, JobDurationMillis
		size(Operations)*size(Attempts) + // AttemptsClaimed
		size(FenceReasons) + // AttemptsFenced
		2*size(Operations) + // QueueLatencyMillis, RecoveryMillis
		size(Topics) + 1 + // OutboxPublished, OutboxPendingAgeSeconds
		size(ReconcileActions) + 1 + // ReconcileActions, ReconcileErrors
		size(ArtifactKinds) + 1 // ArtifactsCommitted, ArtifactCommitFailures
	if total > 1024 {
		t.Fatalf("the instrument set can produce %d series", total)
	}
}

func TestRequestsAreRecordedByRouteTemplate(t *testing.T) {
	instance, buffer := provider(t)
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/jobs/{jobID}", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	handler := instance.Metrics().Instrument("public", mux)
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/v1/jobs/018f7f28-df62-7bc4-9f45-6e6c32a19485", nil))
	if err := instance.Flush(context.Background()); err != nil {
		t.Fatal(err)
	}
	body := buffer.String()
	if strings.Contains(body, "018f7f28-df62-7bc4-9f45-6e6c32a19485") {
		t.Fatalf("a request identifier reached the telemetry stream: %s", body)
	}
	found := false
	for _, record := range lines(t, buffer) {
		if record["Route"] == "GET /v1/jobs/{jobID}" && record["StatusClass"] == "2xx" && record["Surface"] == "public" {
			found = true
		}
	}
	if !found {
		t.Fatalf("the route template was not recorded: %s", body)
	}
}

func TestInstrumentedHandlerStaysFlushable(t *testing.T) {
	instance, _ := provider(t)
	mux := http.NewServeMux()
	flushed := false
	mux.HandleFunc("GET /v1/jobs/{jobID}/events", func(w http.ResponseWriter, _ *http.Request) {
		_, flushed = w.(http.Flusher)
	})
	handler := instance.Metrics().Instrument("public", mux)
	handler.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/v1/jobs/x/events", nil))
	if !flushed {
		t.Fatal("progress streaming lost its flusher")
	}
}
