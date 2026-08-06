package notify_test

import (
	"context"
	"testing"

	"github.com/amln19/locus/controlplane/internal/awstest"
	"github.com/amln19/locus/controlplane/internal/notify"
)

func TestPublishDeliversPayloadAndReportsFailure(t *testing.T) {
	queue := awstest.NewSQS()
	defer queue.Close()
	publisher, err := notify.NewSQS(awstest.Config(queue.Server.URL), queue.QueueURL)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	payload := []byte(`{"protocol_version":1,"job_id":"j","job_version":1,"operation":"diff"}`)
	if err := publisher.Publish(ctx, payload); err != nil {
		t.Fatal(err)
	}
	bodies := queue.Bodies()
	if len(bodies) != 1 || bodies[0] != string(payload) {
		t.Fatalf("bodies=%v", bodies)
	}
	queue.FailNextSend()
	if err := publisher.Publish(ctx, payload); err == nil {
		t.Fatal("queue outage was reported as success")
	}
	if len(queue.Bodies()) != 1 {
		t.Fatalf("failed publish delivered a message: %v", queue.Bodies())
	}
}

func TestNewSQSRequiresQueueURL(t *testing.T) {
	if _, err := notify.NewSQS(awstest.Config("http://127.0.0.1:1"), "  "); err == nil {
		t.Fatal("empty queue URL accepted")
	}
}
