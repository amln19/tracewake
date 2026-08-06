package awstest

import (
	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
)

// Config points the AWS SDK at a fake endpoint with fixed credentials so
// signing runs exactly as it does against the real services.
func Config(endpoint string) aws.Config {
	return aws.Config{
		Region:       "us-east-1",
		Credentials:  credentials.NewStaticCredentialsProvider("test-access-key", "test-secret-key", ""),
		BaseEndpoint: aws.String(endpoint),
	}
}
