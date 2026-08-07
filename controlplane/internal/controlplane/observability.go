package controlplane

import (
	"context"
	"encoding/json"

	"github.com/amln19/locus/controlplane/internal/telemetry"
)

// notificationPayload builds the queue message for a job transition. The
// message carries the current trace context so the worker's execution joins
// the trace that created the work rather than starting an unrelated one.
func notificationPayload(ctx context.Context, jobID string, version int64, operation string) ([]byte, error) {
	message := map[string]any{
		"protocol_version": 1,
		"job_id":           jobID,
		"job_version":      version,
		"operation":        operation,
	}
	if parent := telemetry.Traceparent(ctx); parent != "" {
		message["traceparent"] = parent
	}
	return json.Marshal(message)
}
