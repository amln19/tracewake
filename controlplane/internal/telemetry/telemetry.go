// Package telemetry carries operational evidence about the control plane:
// traces of the lifecycle paths and bounded metrics an operator can alarm on.
// It is unrelated to the OTLP artifacts Locus produces for tenants, which
// describe a recorded run rather than this service.
package telemetry

import (
	"context"
	"errors"
	"io"
	"os"
	"strings"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/propagation"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

// ScopeName identifies spans this service emits, so a collector can separate
// them from spans produced by any library that shares the pipeline.
const ScopeName = "github.com/amln19/locus/controlplane"

type Options struct {
	ServiceName    string
	ServiceVersion string
	Environment    string
	Namespace      string
	// Writer receives one JSON record per line. Records go to standard output
	// by default because the hosted deployment ships container output to
	// CloudWatch Logs and needs no separate agent.
	Writer         io.Writer
	MetricInterval time.Duration
	// Synchronous exports each span as it ends. Batching hides ordering that
	// tests and short-lived commands depend on.
	Synchronous bool
}

type Provider struct {
	traces  *sdktrace.TracerProvider
	metrics *sdkmetric.MeterProvider
	metric  *Metrics
}

// Start installs the global tracer, meter, and W3C propagator. A caller that
// wants no telemetry uses Disabled instead.
func Start(ctx context.Context, options Options) (*Provider, error) {
	if options.Writer == nil {
		options.Writer = os.Stdout
	}
	if options.MetricInterval <= 0 {
		options.MetricInterval = time.Minute
	}
	if strings.TrimSpace(options.Namespace) == "" {
		options.Namespace = "Locus/ControlPlane"
	}
	attributes := resourceAttributes(options)
	details, err := resource.Merge(resource.Default(), resource.NewWithAttributes(resource.Default().SchemaURL(), attributes...))
	if err != nil {
		return nil, err
	}
	var processor sdktrace.SpanProcessor = sdktrace.NewBatchSpanProcessor(&spanExporter{writer: options.Writer})
	if options.Synchronous {
		processor = sdktrace.NewSimpleSpanProcessor(&spanExporter{writer: options.Writer})
	}
	traces := sdktrace.NewTracerProvider(sdktrace.WithResource(details), sdktrace.WithSpanProcessor(processor))
	reader := sdkmetric.NewPeriodicReader(
		newMetricExporter(options.Writer, options.Namespace, options.ServiceName, options.Environment),
		sdkmetric.WithInterval(options.MetricInterval),
	)
	metrics := sdkmetric.NewMeterProvider(sdkmetric.WithResource(details), sdkmetric.WithReader(reader))
	recorder, err := newMetrics(metrics.Meter(ScopeName))
	if err != nil {
		return nil, err
	}
	otel.SetTracerProvider(traces)
	otel.SetTextMapPropagator(propagation.TraceContext{})
	return &Provider{traces: traces, metrics: metrics, metric: recorder}, nil
}

// Disabled returns a provider that records nothing, for local commands and
// tests that do not want a telemetry stream.
func Disabled() *Provider {
	otel.SetTextMapPropagator(propagation.TraceContext{})
	return &Provider{metric: disabledMetrics()}
}

func (p *Provider) Metrics() *Metrics { return p.metric }

func (p *Provider) Tracer() trace.Tracer {
	if p.traces == nil {
		return otel.Tracer(ScopeName)
	}
	return p.traces.Tracer(ScopeName)
}

// Flush exports everything recorded so far. Evidence collection reads the
// stream immediately after an action rather than waiting for the interval.
func (p *Provider) Flush(ctx context.Context) error {
	var problems []error
	if p.traces != nil {
		problems = append(problems, p.traces.ForceFlush(ctx))
	}
	if p.metrics != nil {
		problems = append(problems, p.metrics.ForceFlush(ctx))
	}
	return errors.Join(problems...)
}

func (p *Provider) Shutdown(ctx context.Context) error {
	var problems []error
	if p.traces != nil {
		problems = append(problems, p.traces.Shutdown(ctx))
	}
	if p.metrics != nil {
		problems = append(problems, p.metrics.Shutdown(ctx))
	}
	return errors.Join(problems...)
}

// Span begins a span in this service's scope using the ambient provider, so
// call sites need only the context they already carry.
func Span(ctx context.Context, name string, kind trace.SpanKind) (context.Context, trace.Span) {
	return otel.Tracer(ScopeName).Start(ctx, name, trace.WithSpanKind(kind))
}

// Traceparent renders the active trace context as a W3C header value, which is
// how a trace survives the asynchronous handoff through the queue.
func Traceparent(ctx context.Context) string {
	carrier := propagation.MapCarrier{}
	otel.GetTextMapPropagator().Inject(ctx, carrier)
	return carrier.Get("traceparent")
}

// Continue resumes the trace a traceparent names. An empty or malformed value
// leaves the context alone rather than failing the work it accompanies.
func Continue(ctx context.Context, parent string) context.Context {
	if parent == "" {
		return ctx
	}
	return otel.GetTextMapPropagator().Extract(ctx, propagation.MapCarrier{"traceparent": parent})
}

func resourceAttributes(options Options) []attribute.KeyValue {
	return []attribute.KeyValue{
		attribute.String("service.name", nonEmpty(options.ServiceName, "locus-control-plane")),
		attribute.String("service.version", nonEmpty(options.ServiceVersion, "unknown")),
		attribute.String("deployment.environment", nonEmpty(options.Environment, "local")),
	}
}

func nonEmpty(value, fallback string) string {
	if strings.TrimSpace(value) == "" {
		return fallback
	}
	return value
}
