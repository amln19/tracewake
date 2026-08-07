package telemetry

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

type alarmSpecification struct {
	Alarms []struct {
		Name       string            `json:"name"`
		Namespace  string            `json:"namespace"`
		MetricName string            `json:"metric_name"`
		Dimensions map[string]string `json:"dimensions"`
	} `json:"alarms"`
}

func loadAlarms(t *testing.T) alarmSpecification {
	t.Helper()
	path := filepath.Join("..", "..", "..", "deploy", "aws", "alarms.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read alarm specification: %v", err)
	}
	var specification alarmSpecification
	if err := json.Unmarshal(raw, &specification); err != nil {
		t.Fatal(err)
	}
	return specification
}

// An alarm on a metric the service never emits is worse than no alarm: it
// stays silent and reads as health. Driving every instrument and matching the
// exported stream against the deployed thresholds keeps the two in step.
func TestEveryControlPlaneAlarmWatchesAnEmittedMetric(t *testing.T) {
	instance, buffer := provider(t)
	ctx := context.Background()
	recorder := instance.Metrics()
	recorder.HTTPRequest(ctx, "public", "POST /v1/jobs", 500, time.Second)
	recorder.JobCreated(ctx, "diff")
	recorder.JobTerminal(ctx, "diff", "failed", time.Second)
	recorder.AttemptClaimed(ctx, "diff", 1, 90*time.Second, 0)
	recorder.AttemptClaimed(ctx, "diff", 2, 0, 30*time.Second)
	recorder.AttemptFenced(ctx, "lease_expired")
	recorder.AttemptFenced(ctx, "retry_exhausted")
	recorder.OutboxPublished(ctx, "job.created", 1)
	recorder.OutboxPendingAge(ctx, 130*time.Second)
	recorder.ReconcileAction(ctx, "lease_fenced", 1)
	recorder.ReconcileFailed(ctx)
	recorder.ArtifactCommitted(ctx, "diff_json")
	recorder.ArtifactCommitFailed(ctx)
	if err := instance.Flush(ctx); err != nil {
		t.Fatal(err)
	}

	emitted := map[string][]map[string]any{}
	for _, record := range lines(t, buffer) {
		metadata := record["_aws"].(map[string]any)
		for _, directive := range metadata["CloudWatchMetrics"].([]any) {
			for _, definition := range directive.(map[string]any)["Metrics"].([]any) {
				name := definition.(map[string]any)["Name"].(string)
				emitted[name] = append(emitted[name], record)
			}
		}
	}

	checked := 0
	for _, alarm := range loadAlarms(t).Alarms {
		if alarm.Namespace != "@control_plane" {
			continue
		}
		checked++
		candidates, ok := emitted[alarm.MetricName]
		if !ok {
			t.Errorf("alarm %q watches %q, which this service never emits", alarm.Name, alarm.MetricName)
			continue
		}
		matched := false
		for _, candidate := range candidates {
			agrees := true
			for key, value := range alarm.Dimensions {
				if candidate[key] != value {
					agrees = false
					break
				}
			}
			if agrees {
				matched = true
				break
			}
		}
		if !matched {
			t.Errorf("alarm %q watches %q with dimensions %v, which no emitted record carries", alarm.Name, alarm.MetricName, alarm.Dimensions)
		}
	}
	if checked == 0 {
		t.Fatal("the alarm specification names no control-plane metric")
	}
}
