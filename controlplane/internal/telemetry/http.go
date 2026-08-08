package telemetry

import (
	"net/http"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
)

// Instrument names a request by the route it matched rather than the path it
// used, so identifiers never reach the metric stream. The pattern is resolved
// before the handler runs because middleware cannot see what the mux matched.
func (m *Metrics) Instrument(surface string, mux *http.ServeMux) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, route := mux.Handler(r)
		if route == "" {
			route = otherValue
		}
		ctx := otel.GetTextMapPropagator().Extract(r.Context(), propagation.HeaderCarrier(r.Header))
		ctx, span := otel.Tracer(ScopeName).Start(ctx, route, trace.WithSpanKind(trace.SpanKindServer),
			trace.WithAttributes(
				attribute.String("http.request.method", r.Method),
				attribute.String("http.route", bounded(route, Routes)),
				attribute.String("tracewake.surface", surface),
			))
		defer span.End()
		recorder := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		started := time.Now()
		mux.ServeHTTP(recorder, r.WithContext(ctx))
		elapsed := time.Since(started)
		span.SetAttributes(attribute.Int("http.response.status_code", recorder.status))
		if recorder.status >= 500 {
			span.SetStatus(codes.Error, "request failed")
		}
		m.HTTPRequest(ctx, surface, route, recorder.status, elapsed)
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status  int
	written bool
}

func (r *statusRecorder) WriteHeader(status int) {
	if !r.written {
		r.status = status
		r.written = true
	}
	r.ResponseWriter.WriteHeader(status)
}

func (r *statusRecorder) Write(data []byte) (int, error) {
	r.written = true
	return r.ResponseWriter.Write(data)
}

// Flush keeps the progress stream incremental; wrapping a response writer
// otherwise buffers server-sent events until the handler returns.
func (r *statusRecorder) Flush() {
	if flusher, ok := r.ResponseWriter.(http.Flusher); ok {
		flusher.Flush()
	}
}
