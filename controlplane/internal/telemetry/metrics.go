package telemetry

import (
	"context"
	"encoding/json"
	"io"
	"sort"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/metric/noop"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
)

// Every dimension value comes from one of these sets. An unrecognised value
// becomes "other" rather than a new time series, which is what keeps a metric
// bill and an alarm evaluation bounded no matter what a client sends.
var (
	Operations       = []string{"validate", "diff", "otlp", "pprof"}
	Outcomes         = []string{"succeeded", "failed", "cancelled"}
	FenceReasons     = []string{"lease_expired", "retryable_failure", "retry_exhausted", "cancelled"}
	ReconcileActions = []string{"lease_fenced", "retry_scheduled", "retry_exhausted", "republished"}
	ArtifactKinds    = []string{"validation_json", "diff_json", "diff_html", "otlp_json", "otlp_result_json", "pprof", "pprof_result_json", "worker_diagnostic"}
	Surfaces         = []string{"public", "worker", "object"}
	StatusClasses    = []string{"2xx", "3xx", "4xx", "5xx"}
	Attempts         = []string{"1", "2", "3"}
	Topics           = []string{"job.created"}
)

const otherValue = "other"

// Routes are the request templates this service answers. Recording the
// template rather than the path keeps identifiers out of the metric stream.
var Routes = []string{
	"POST /v1/browser/sessions", "GET /v1/browser/session", "DELETE /v1/browser/session",
	"GET /v1/browser/artifacts/{artifactID}", "POST /v1/browser/runs/uploads", "PUT /v1/browser/runs/uploads/{runID}",
	"POST /v1/runs/uploads", "POST /v1/runs/uploads/{runID}/complete", "GET /v1/runs", "GET /v1/runs/{runID}", "DELETE /v1/runs/{runID}",
	"POST /v1/jobs", "GET /v1/jobs/{jobID}", "POST /v1/jobs/{jobID}/cancel", "GET /v1/jobs/{jobID}/events",
	"GET /v1/artifacts/{artifactID}/download", "GET /v1/audit",
	"GET /internal/v1/identity", "GET /internal/v1/notifications/next", "POST /internal/v1/notifications/{id}/ack",
	"POST /internal/v1/claims",
	"PUT /internal/v1/jobs/{job}/attempts/{attempt}/heartbeat",
	"PUT /internal/v1/jobs/{job}/attempts/{attempt}/progress",
	"GET /internal/v1/jobs/{job}/attempts/{attempt}/cancellation",
	"POST /internal/v1/jobs/{job}/attempts/{attempt}/fail",
	"POST /internal/v1/jobs/{job}/attempts/{attempt}/artifacts",
	"POST /internal/v1/jobs/{job}/attempts/{attempt}/complete",
	"GET /internal/v1/jobs/{job}/attempts/{attempt}/inputs/{artifact}",
}

func bounded(value string, allowed []string) string {
	for _, candidate := range allowed {
		if candidate == value {
			return value
		}
	}
	return otherValue
}

type Metrics struct {
	httpRequests           metric.Int64Counter
	httpDuration           metric.Float64Histogram
	jobsCreated            metric.Int64Counter
	jobsTerminal           metric.Int64Counter
	jobDuration            metric.Float64Histogram
	attemptsClaimed        metric.Int64Counter
	attemptsFenced         metric.Int64Counter
	queueLatency           metric.Float64Histogram
	recovery               metric.Float64Histogram
	outboxPublished        metric.Int64Counter
	outboxPendingAge       metric.Float64Gauge
	reconcileActions       metric.Int64Counter
	reconcileErrors        metric.Int64Counter
	artifactsCommitted     metric.Int64Counter
	artifactCommitFailures metric.Int64Counter
}

func newMetrics(meter metric.Meter) (*Metrics, error) {
	var problem error
	counter := func(name, description string) metric.Int64Counter {
		instrument, err := meter.Int64Counter(name, metric.WithDescription(description), metric.WithUnit("1"))
		if err != nil {
			problem = err
		}
		return instrument
	}
	histogram := func(name, description string) metric.Float64Histogram {
		instrument, err := meter.Float64Histogram(name, metric.WithDescription(description), metric.WithUnit("ms"))
		if err != nil {
			problem = err
		}
		return instrument
	}
	metrics := &Metrics{
		httpRequests:           counter("HttpRequests", "Requests answered, by surface, route template, and status class"),
		httpDuration:           histogram("HttpDurationMillis", "Request handling time"),
		jobsCreated:            counter("JobsCreated", "Jobs accepted into the lifecycle"),
		jobsTerminal:           counter("JobsTerminal", "Jobs that reached an immutable terminal state"),
		jobDuration:            histogram("JobDurationMillis", "Time from job creation to terminal state"),
		attemptsClaimed:        counter("AttemptsClaimed", "Attempts started by a worker claim"),
		attemptsFenced:         counter("AttemptsFenced", "Attempts fenced so they can no longer commit"),
		queueLatency:           histogram("QueueLatencyMillis", "Time from job creation to the claim that started work"),
		recovery:               histogram("RecoveryMillis", "Time from a fenced attempt to the claim that replaced it"),
		outboxPublished:        counter("OutboxPublished", "Outbox rows published to the notification queue"),
		reconcileActions:       counter("ReconcileActions", "Conditional repairs the reconciler applied"),
		reconcileErrors:        counter("ReconcileErrors", "Reconciler passes that failed"),
		artifactsCommitted:     counter("ArtifactsCommitted", "Immutable artifacts committed to a successful attempt"),
		artifactCommitFailures: counter("ArtifactCommitFailures", "Artifact commits refused because identity did not match"),
	}
	gauge, err := meter.Float64Gauge("OutboxPendingAgeSeconds", metric.WithDescription("Age of the oldest unpublished outbox row"), metric.WithUnit("s"))
	if err != nil {
		return nil, err
	}
	metrics.outboxPendingAge = gauge
	if problem != nil {
		return nil, problem
	}
	return metrics, nil
}

// NoMetrics records nothing. Components take a recorder rather than a nullable
// pointer so no call site has to guard every measurement.
func NoMetrics() *Metrics { return disabledMetrics() }

func disabledMetrics() *Metrics {
	metrics, err := newMetrics(noop.NewMeterProvider().Meter(ScopeName))
	if err != nil {
		panic(err)
	}
	return metrics
}

func (m *Metrics) HTTPRequest(ctx context.Context, surface, route string, status int, elapsed time.Duration) {
	class := StatusClasses[0]
	switch {
	case status >= 500:
		class = "5xx"
	case status >= 400:
		class = "4xx"
	case status >= 300:
		class = "3xx"
	}
	surfaceValue := attribute.String("Surface", bounded(surface, Surfaces))
	routeValue := attribute.String("Route", bounded(route, Routes))
	m.httpRequests.Add(ctx, 1, metric.WithAttributes(surfaceValue, routeValue, attribute.String("StatusClass", class)))
	m.httpDuration.Record(ctx, millis(elapsed), metric.WithAttributes(surfaceValue, routeValue))
}

func (m *Metrics) JobCreated(ctx context.Context, operation string) {
	m.jobsCreated.Add(ctx, 1, metric.WithAttributes(attribute.String("Operation", bounded(operation, Operations))))
}

func (m *Metrics) JobTerminal(ctx context.Context, operation, outcome string, since time.Duration) {
	attributes := metric.WithAttributes(
		attribute.String("Operation", bounded(operation, Operations)),
		attribute.String("Outcome", bounded(outcome, Outcomes)),
	)
	m.jobsTerminal.Add(ctx, 1, attributes)
	if since > 0 {
		m.jobDuration.Record(ctx, millis(since), attributes)
	}
}

func (m *Metrics) AttemptClaimed(ctx context.Context, operation string, attempt int, queued, recovered time.Duration) {
	operationValue := attribute.String("Operation", bounded(operation, Operations))
	m.attemptsClaimed.Add(ctx, 1, metric.WithAttributes(operationValue, attribute.String("Attempt", bounded(itoa(attempt), Attempts))))
	if attempt == 1 && queued > 0 {
		m.queueLatency.Record(ctx, millis(queued), metric.WithAttributes(operationValue))
	}
	if attempt > 1 && recovered > 0 {
		m.recovery.Record(ctx, millis(recovered), metric.WithAttributes(operationValue))
	}
}

func (m *Metrics) AttemptFenced(ctx context.Context, reason string) {
	m.attemptsFenced.Add(ctx, 1, metric.WithAttributes(attribute.String("Reason", bounded(reason, FenceReasons))))
}

func (m *Metrics) OutboxPublished(ctx context.Context, topic string, count int) {
	if count > 0 {
		m.outboxPublished.Add(ctx, int64(count), metric.WithAttributes(attribute.String("Topic", bounded(topic, Topics))))
	}
}

func (m *Metrics) OutboxPendingAge(ctx context.Context, age time.Duration) {
	m.outboxPendingAge.Record(ctx, age.Seconds())
}

func (m *Metrics) ReconcileAction(ctx context.Context, action string, count int) {
	if count > 0 {
		m.reconcileActions.Add(ctx, int64(count), metric.WithAttributes(attribute.String("Action", bounded(action, ReconcileActions))))
	}
}

func (m *Metrics) ReconcileFailed(ctx context.Context) { m.reconcileErrors.Add(ctx, 1) }

func (m *Metrics) ArtifactCommitted(ctx context.Context, kind string) {
	m.artifactsCommitted.Add(ctx, 1, metric.WithAttributes(attribute.String("Kind", bounded(kind, ArtifactKinds))))
}

func (m *Metrics) ArtifactCommitFailed(ctx context.Context) { m.artifactCommitFailures.Add(ctx, 1) }

func millis(value time.Duration) float64 { return float64(value) / float64(time.Millisecond) }

func itoa(value int) string {
	if value < 0 || value > 9 {
		return otherValue
	}
	return string(rune('0' + value))
}

// metricExporter writes CloudWatch embedded metric format, so container output
// alone produces alarmable metrics without a metrics agent or collector.
type metricExporter struct {
	writer      io.Writer
	namespace   string
	service     string
	environment string
	mutex       sync.Mutex
}

func newMetricExporter(writer io.Writer, namespace, service, environment string) *metricExporter {
	return &metricExporter{writer: writer, namespace: namespace, service: nonEmpty(service, "tracewake-control-plane"), environment: nonEmpty(environment, "local")}
}

func (e *metricExporter) Temporality(kind sdkmetric.InstrumentKind) metricdata.Temporality {
	if kind == sdkmetric.InstrumentKindGauge {
		return metricdata.CumulativeTemporality
	}
	return metricdata.DeltaTemporality
}

func (e *metricExporter) Aggregation(kind sdkmetric.InstrumentKind) sdkmetric.Aggregation {
	return sdkmetric.DefaultAggregationSelector(kind)
}

func (e *metricExporter) ForceFlush(context.Context) error { return nil }
func (e *metricExporter) Shutdown(context.Context) error   { return nil }

type emfDefinition struct {
	Name string `json:"Name"`
	Unit string `json:"Unit"`
}

type emfDirective struct {
	Namespace  string          `json:"Namespace"`
	Dimensions [][]string      `json:"Dimensions"`
	Metrics    []emfDefinition `json:"Metrics"`
}

type emfMetadata struct {
	Timestamp int64          `json:"Timestamp"`
	Metrics   []emfDirective `json:"CloudWatchMetrics"`
}

type emfGroup struct {
	dimensions  []string
	values      map[string]string
	definitions []emfDefinition
	fields      map[string]any
}

func (e *metricExporter) Export(_ context.Context, data *metricdata.ResourceMetrics) error {
	groups := map[string]*emfGroup{}
	order := []string{}
	add := func(set attribute.Set, definition emfDefinition, value any) {
		names := make([]string, 0, set.Len())
		values := map[string]string{}
		for _, item := range set.ToSlice() {
			names = append(names, string(item.Key))
			values[string(item.Key)] = item.Value.Emit()
		}
		sort.Strings(names)
		key := definitionKey(names, values)
		group, seen := groups[key]
		if !seen {
			group = &emfGroup{dimensions: names, values: values, fields: map[string]any{}}
			groups[key] = group
			order = append(order, key)
		}
		group.definitions = append(group.definitions, definition)
		group.fields[definition.Name] = value
	}
	for _, scope := range data.ScopeMetrics {
		for _, item := range scope.Metrics {
			definition := emfDefinition{Name: item.Name, Unit: emfUnit(item.Unit)}
			switch aggregation := item.Data.(type) {
			case metricdata.Sum[int64]:
				for _, point := range aggregation.DataPoints {
					if point.Value != 0 {
						add(point.Attributes, definition, point.Value)
					}
				}
			case metricdata.Gauge[float64]:
				for _, point := range aggregation.DataPoints {
					add(point.Attributes, definition, point.Value)
				}
			case metricdata.Histogram[float64]:
				for _, point := range aggregation.DataPoints {
					if point.Count == 0 {
						continue
					}
					statistics := map[string]any{"Count": point.Count, "Sum": point.Sum}
					if value, ok := point.Min.Value(); ok {
						statistics["Min"] = value
					}
					if value, ok := point.Max.Value(); ok {
						statistics["Max"] = value
					}
					add(point.Attributes, definition, statistics)
				}
			}
		}
	}
	if len(order) == 0 {
		return nil
	}
	e.mutex.Lock()
	defer e.mutex.Unlock()
	encoder := json.NewEncoder(e.writer)
	timestamp := time.Now().UnixMilli()
	for _, key := range order {
		group := groups[key]
		record := map[string]any{
			"_aws": emfMetadata{
				Timestamp: timestamp,
				Metrics:   []emfDirective{{Namespace: e.namespace, Dimensions: [][]string{group.dimensions}, Metrics: group.definitions}},
			},
			"telemetry":              "metric",
			"service_name":           e.service,
			"deployment_environment": e.environment,
		}
		for name, value := range group.values {
			record[name] = value
		}
		for name, value := range group.fields {
			record[name] = value
		}
		if err := encoder.Encode(record); err != nil {
			return err
		}
	}
	return nil
}

func definitionKey(names []string, values map[string]string) string {
	key := ""
	for _, name := range names {
		key += name + "=" + values[name] + ";"
	}
	return key
}

func emfUnit(unit string) string {
	switch unit {
	case "ms":
		return "Milliseconds"
	case "s":
		return "Seconds"
	default:
		return "Count"
	}
}
