package controlplane_test

import (
	"context"
	"encoding/json"
	"errors"
	"testing"

	"github.com/amln19/locus/controlplane/internal/awstest"
	"github.com/amln19/locus/controlplane/internal/controlplane"
	"github.com/amln19/locus/controlplane/internal/notify"
)

type failingNotifier struct{ published int }

func (f *failingNotifier) Publish(context.Context, []byte) error {
	f.published++
	return errors.New("queue is unavailable")
}

func TestOutboxPublishesToQueueAndSurvivesFailure(t *testing.T) {
	service, pool, workspace := newTestService(t)
	ctx := context.Background()
	queue := awstest.NewSQS()
	defer queue.Close()
	publisher, err := notify.NewSQS(awstest.Config(queue.Server.URL), queue.QueueURL)
	if err != nil {
		t.Fatal(err)
	}
	runA, runB := readyRun(t, pool, workspace), readyRun(t, pool, workspace)
	principal := controlplane.Principal{WorkspaceID: workspace, Scopes: map[string]bool{"jobs:write": true}}
	profile := "lexical-v1"
	job, _, err := service.CreateJob(ctx, principal, "outbox-"+runA, controlplane.JobRequest{Operation: "diff", RunIDs: []string{runA, runB}, Profile: &profile})
	if err != nil {
		t.Fatal(err)
	}

	broken := &failingNotifier{}
	if _, err := service.PublishOutbox(ctx, broken, 100); err == nil {
		t.Fatal("queue outage reported as success")
	}
	var unpublished int
	if err := pool.QueryRow(ctx, "SELECT count(*) FROM outbox WHERE aggregate_id=$1 AND published_at IS NULL", job.ID).Scan(&unpublished); err != nil {
		t.Fatal(err)
	}
	if unpublished != 1 {
		t.Fatalf("failed publication marked the row published: unpublished=%d", unpublished)
	}

	if _, err := service.PublishOutbox(ctx, publisher, 100); err != nil {
		t.Fatal(err)
	}
	var delivered []string
	for _, body := range queue.Bodies() {
		var payload struct {
			ProtocolVersion int    `json:"protocol_version"`
			JobID           string `json:"job_id"`
			Operation       string `json:"operation"`
		}
		if err := json.Unmarshal([]byte(body), &payload); err != nil {
			t.Fatal(err)
		}
		if payload.ProtocolVersion != 1 || payload.Operation == "" {
			t.Fatalf("payload=%s", body)
		}
		delivered = append(delivered, payload.JobID)
	}
	if len(delivered) == 0 {
		t.Fatal("no notification reached the queue")
	}

	before := len(queue.Bodies())
	if published, err := service.PublishOutbox(ctx, publisher, 100); err != nil || published != 0 {
		t.Fatalf("republished %d rows: %v", published, err)
	}
	if len(queue.Bodies()) != before {
		t.Fatal("published rows were delivered twice")
	}
}

func TestDuplicateDeliveryCreatesOneAttempt(t *testing.T) {
	service, pool, workspace := newTestService(t)
	ctx := context.Background()
	queue := awstest.NewSQS()
	defer queue.Close()
	publisher, err := notify.NewSQS(awstest.Config(queue.Server.URL), queue.QueueURL)
	if err != nil {
		t.Fatal(err)
	}
	runA, runB := readyRun(t, pool, workspace), readyRun(t, pool, workspace)
	principal := controlplane.Principal{WorkspaceID: workspace, Scopes: map[string]bool{"jobs:write": true}}
	profile := "lexical-v1"
	job, _, err := service.CreateJob(ctx, principal, "duplicate-"+runA, controlplane.JobRequest{Operation: "diff", RunIDs: []string{runA, runB}, Profile: &profile})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.PublishOutbox(ctx, publisher, 100); err != nil {
		t.Fatal(err)
	}
	// A publisher crash after sending but before recording publication makes the
	// same notification arrive twice.
	if _, err := pool.Exec(ctx, "UPDATE outbox SET published_at=NULL WHERE aggregate_id=$1", job.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := service.PublishOutbox(ctx, publisher, 100); err != nil {
		t.Fatal(err)
	}
	if len(queue.Bodies()) < 2 {
		t.Fatalf("expected a duplicate delivery, got %d", len(queue.Bodies()))
	}
	worker, _, err := service.CreateWorkerCredential(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var version int64
	if err := pool.QueryRow(ctx, "SELECT row_version FROM jobs WHERE id=$1", job.ID).Scan(&version); err != nil {
		t.Fatal(err)
	}
	if _, err := service.ClaimNotification(ctx, worker, job.ID, version); err != nil {
		t.Fatal(err)
	}
	if _, err := service.ClaimNotification(ctx, worker, job.ID, version); !errors.Is(err, controlplane.ErrConflict) {
		t.Fatalf("duplicate delivery claimed twice: %v", err)
	}
	var attempts int
	if err := pool.QueryRow(ctx, "SELECT count(*) FROM job_attempts WHERE job_id=$1", job.ID).Scan(&attempts); err != nil {
		t.Fatal(err)
	}
	if attempts != 1 {
		t.Fatalf("attempts=%d", attempts)
	}
}

func TestOutboxPayloadsCarryNoSensitiveContent(t *testing.T) {
	service, pool, workspace := newTestService(t)
	ctx := context.Background()
	runA, runB := readyRun(t, pool, workspace), readyRun(t, pool, workspace)
	principal := controlplane.Principal{WorkspaceID: workspace, Scopes: map[string]bool{"jobs:write": true}}
	profile := "lexical-v1"
	job, _, err := service.CreateJob(ctx, principal, "payload-"+runA, controlplane.JobRequest{Operation: "diff", RunIDs: []string{runA, runB}, Profile: &profile})
	if err != nil {
		t.Fatal(err)
	}
	var payload map[string]any
	if err := pool.QueryRow(ctx, "SELECT payload FROM outbox WHERE aggregate_id=$1", job.ID).Scan(&payload); err != nil {
		t.Fatal(err)
	}
	allowed := map[string]bool{"protocol_version": true, "job_id": true, "job_version": true, "operation": true}
	for field := range payload {
		if !allowed[field] {
			t.Fatalf("notification carries %q", field)
		}
	}
}
