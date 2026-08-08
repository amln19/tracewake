package telemetry

import (
	"context"
	"encoding/json"
	"io"
	"sync"
	"time"

	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// SpanRecord is the line format both the control plane and the Python worker
// emit, so one reader can reconstruct a trace that crosses the language
// boundary. Field names follow the OTLP span model.
type SpanRecord struct {
	Telemetry    string            `json:"telemetry"`
	Scope        string            `json:"scope"`
	Service      string            `json:"service_name"`
	Environment  string            `json:"deployment_environment"`
	TraceID      string            `json:"trace_id"`
	SpanID       string            `json:"span_id"`
	ParentSpanID string            `json:"parent_span_id,omitempty"`
	Name         string            `json:"name"`
	Kind         string            `json:"kind"`
	StartTime    string            `json:"start_time"`
	EndTime      string            `json:"end_time"`
	DurationMs   float64           `json:"duration_ms"`
	Status       string            `json:"status"`
	Attributes   map[string]any    `json:"attributes,omitempty"`
	Resource     map[string]string `json:"resource,omitempty"`
}

// IdleAttribute marks a span that looked for work and found none. Polling
// loops would otherwise dominate the trace stream and its cost while saying
// nothing; an idle span that failed is still exported, because that is work.
const IdleAttribute = "tracewake.idle"

func idle(span sdktrace.ReadOnlySpan) bool {
	if span.Status().Code == codes.Error {
		return false
	}
	for _, item := range span.Attributes() {
		if string(item.Key) == IdleAttribute {
			return item.Value.AsBool()
		}
	}
	return false
}

type spanExporter struct {
	writer io.Writer
	mutex  sync.Mutex
}

func (e *spanExporter) ExportSpans(_ context.Context, spans []sdktrace.ReadOnlySpan) error {
	e.mutex.Lock()
	defer e.mutex.Unlock()
	encoder := json.NewEncoder(e.writer)
	for _, span := range spans {
		if idle(span) {
			continue
		}
		if err := encoder.Encode(spanRecord(span)); err != nil {
			return err
		}
	}
	return nil
}

func (e *spanExporter) Shutdown(context.Context) error { return nil }

func spanRecord(span sdktrace.ReadOnlySpan) SpanRecord {
	context := span.SpanContext()
	record := SpanRecord{
		Telemetry:  "span",
		Scope:      span.InstrumentationScope().Name,
		TraceID:    context.TraceID().String(),
		SpanID:     context.SpanID().String(),
		Name:       span.Name(),
		Kind:       span.SpanKind().String(),
		StartTime:  span.StartTime().UTC().Format(time.RFC3339Nano),
		EndTime:    span.EndTime().UTC().Format(time.RFC3339Nano),
		DurationMs: float64(span.EndTime().Sub(span.StartTime())) / float64(time.Millisecond),
		Status:     span.Status().Code.String(),
		Resource:   map[string]string{},
	}
	if parent := span.Parent(); parent.IsValid() {
		record.ParentSpanID = parent.SpanID().String()
	}
	if attributes := span.Attributes(); len(attributes) > 0 {
		record.Attributes = make(map[string]any, len(attributes))
		for _, item := range attributes {
			record.Attributes[string(item.Key)] = item.Value.AsInterface()
		}
	}
	for _, item := range span.Resource().Attributes() {
		switch string(item.Key) {
		case "service.name":
			record.Service = item.Value.AsString()
		case "deployment.environment":
			record.Environment = item.Value.AsString()
		case "service.version":
			record.Resource["service.version"] = item.Value.AsString()
		}
	}
	return record
}
