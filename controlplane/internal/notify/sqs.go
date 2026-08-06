// Package notify publishes transactional outbox rows to the hosted
// notification queue. The queue only wakes workers; PostgreSQL remains the
// authority for what work exists.
package notify

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/sqs"
)

type SQS struct {
	client   *sqs.Client
	queueURL string
}

func NewSQS(cfg aws.Config, queueURL string, options ...func(*sqs.Options)) (*SQS, error) {
	if strings.TrimSpace(queueURL) == "" {
		return nil, errors.New("job queue URL is required")
	}
	return &SQS{client: sqs.NewFromConfig(cfg, options...), queueURL: queueURL}, nil
}

func (s *SQS) Publish(ctx context.Context, payload []byte) error {
	if _, err := s.client.SendMessage(ctx, &sqs.SendMessageInput{
		QueueUrl:    aws.String(s.queueURL),
		MessageBody: aws.String(string(payload)),
	}); err != nil {
		return fmt.Errorf("publish job notification: %w", err)
	}
	return nil
}
