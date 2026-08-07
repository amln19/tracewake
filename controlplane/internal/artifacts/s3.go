package artifacts

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
)

// S3 keeps sensitive bytes in a private versioned bucket. Clients reach it
// only through presigned URLs, so no worker or tenant credential can name a
// bucket, key, or version the control plane did not choose.
type S3 struct {
	client  *s3.Client
	presign *s3.PresignClient
	bucket  string
}

func NewS3(cfg aws.Config, bucket string, options ...func(*s3.Options)) (*S3, error) {
	if strings.TrimSpace(bucket) == "" {
		return nil, errors.New("artifact bucket is required")
	}
	client := s3.NewFromConfig(cfg, options...)
	return &S3{client: client, presign: s3.NewPresignClient(client), bucket: bucket}, nil
}

func (s *S3) PutGrant(ctx context.Context, key, digest string, size int64, mediaType string) (Grant, error) {
	checksum, err := checksumOf(digest)
	if err != nil || !validDeclaration(key, digest, size) {
		return Grant{}, errors.New("artifact declaration is invalid")
	}
	request, err := s.presign.PresignPutObject(ctx, &s3.PutObjectInput{
		Bucket:         aws.String(s.bucket),
		Key:            aws.String(key),
		ChecksumSHA256: aws.String(checksum),
		ContentLength:  aws.Int64(size),
		ContentType:    aws.String(mediaType),
	}, s3.WithPresignExpires(GrantLifetime))
	if err != nil {
		return Grant{}, fmt.Errorf("presign artifact upload: %w", err)
	}
	return Grant{
		URL:       request.URL,
		Method:    request.Method,
		Headers:   signedHeaders(request.SignedHeader),
		ExpiresAt: time.Now().UTC().Add(GrantLifetime),
	}, nil
}

func (s *S3) GetGrant(ctx context.Context, key, version, _ string) (Grant, error) {
	if !safeKey(key) || version == "" {
		return Grant{}, errors.New("artifact identity is invalid")
	}
	request, err := s.presign.PresignGetObject(ctx, &s3.GetObjectInput{
		Bucket:                     aws.String(s.bucket),
		Key:                        aws.String(key),
		VersionId:                  aws.String(version),
		ResponseContentDisposition: aws.String("attachment"),
	}, s3.WithPresignExpires(GrantLifetime))
	if err != nil {
		return Grant{}, fmt.Errorf("presign artifact download: %w", err)
	}
	return Grant{
		URL:       request.URL,
		Method:    request.Method,
		Headers:   signedHeaders(request.SignedHeader),
		ExpiresAt: time.Now().UTC().Add(GrantLifetime),
	}, nil
}

// MaxVerifiedReadSize bounds how much the control plane will re-read to prove
// an artifact's digest. Results stay far below it; a bundle above it is proven
// instead by mandatory validation, which rejects a run whose stored bytes do
// not hash to the declared digest.
const MaxVerifiedReadSize int64 = 8 * 1024 * 1024

// Commit confirms the exact object version the caller reported. A presigned
// upload cannot bind a checksum S3 will enforce, so the digest is proven from
// the stored checksum when the store recorded one and from the stored bytes
// otherwise.
func (s *S3) Commit(ctx context.Context, key, version, digest string, size int64) (Object, error) {
	checksum, err := checksumOf(digest)
	if err != nil || !validDeclaration(key, digest, size) || version == "" {
		return Object{}, errors.New("artifact identity is invalid")
	}
	head, err := s.client.HeadObject(ctx, &s3.HeadObjectInput{
		Bucket:       aws.String(s.bucket),
		Key:          aws.String(key),
		VersionId:    aws.String(version),
		ChecksumMode: types.ChecksumModeEnabled,
	})
	if err != nil {
		return Object{}, fmt.Errorf("read stored artifact identity: %w", err)
	}
	if aws.ToString(head.VersionId) != version {
		return Object{}, errors.New("stored artifact version does not match the reported version")
	}
	if aws.ToInt64(head.ContentLength) != size {
		return Object{}, errors.New("artifact identity does not match stored bytes")
	}
	if stored := aws.ToString(head.ChecksumSHA256); stored != "" {
		if stored != checksum {
			return Object{}, errors.New("artifact identity does not match stored bytes")
		}
		return Object{Key: key, Version: version, Digest: digest, Size: size}, nil
	}
	if size <= MaxVerifiedReadSize {
		if err := s.verifyStoredBytes(ctx, key, version, digest, size); err != nil {
			return Object{}, err
		}
	}
	return Object{Key: key, Version: version, Digest: digest, Size: size}, nil
}

func (s *S3) Open(ctx context.Context, key, version string) (io.ReadCloser, error) {
	if !safeKey(key) || version == "" {
		return nil, errors.New("artifact identity is invalid")
	}
	object, err := s.client.GetObject(ctx, &s3.GetObjectInput{
		Bucket:    aws.String(s.bucket),
		Key:       aws.String(key),
		VersionId: aws.String(version),
	})
	if err != nil {
		return nil, fmt.Errorf("read stored artifact: %w", err)
	}
	return object.Body, nil
}

func (s *S3) verifyStoredBytes(ctx context.Context, key, version, digest string, size int64) error {
	object, err := s.client.GetObject(ctx, &s3.GetObjectInput{
		Bucket:    aws.String(s.bucket),
		Key:       aws.String(key),
		VersionId: aws.String(version),
	})
	if err != nil {
		return fmt.Errorf("read stored artifact: %w", err)
	}
	defer object.Body.Close()
	hash := sha256.New()
	read, err := io.Copy(hash, io.LimitReader(object.Body, size+1))
	if err != nil {
		return fmt.Errorf("hash stored artifact: %w", err)
	}
	if read != size || hex.EncodeToString(hash.Sum(nil)) != digest {
		return errors.New("artifact identity does not match stored bytes")
	}
	return nil
}

func (s *S3) Cleanup(ctx context.Context, keep map[string]bool, before time.Time) (int, error) {
	removed := 0
	var keyMarker, versionMarker *string
	for {
		page, err := s.client.ListObjectVersions(ctx, &s3.ListObjectVersionsInput{
			Bucket:          aws.String(s.bucket),
			KeyMarker:       keyMarker,
			VersionIdMarker: versionMarker,
		})
		if err != nil {
			return removed, fmt.Errorf("list stored artifacts: %w", err)
		}
		var expired []types.ObjectIdentifier
		for _, object := range page.Versions {
			key := aws.ToString(object.Key)
			if keep[key] || object.LastModified == nil || object.LastModified.After(before) {
				continue
			}
			expired = append(expired, types.ObjectIdentifier{Key: object.Key, VersionId: object.VersionId})
		}
		if len(expired) > 0 {
			if _, err := s.client.DeleteObjects(ctx, &s3.DeleteObjectsInput{
				Bucket: aws.String(s.bucket),
				Delete: &types.Delete{Objects: expired, Quiet: aws.Bool(true)},
			}); err != nil {
				return removed, fmt.Errorf("delete expired artifacts: %w", err)
			}
			removed += len(expired)
		}
		if !aws.ToBool(page.IsTruncated) {
			return removed, nil
		}
		keyMarker, versionMarker = page.NextKeyMarker, page.NextVersionIdMarker
	}
}

func checksumOf(digest string) (string, error) {
	raw, err := hex.DecodeString(digest)
	if err != nil || len(raw) != 32 {
		return "", errors.New("digest must be 64 hexadecimal characters")
	}
	return base64.StdEncoding.EncodeToString(raw), nil
}

// The client must replay every header the signature covers, so the grant
// carries exactly what the presigner bound.
func signedHeaders(signed map[string][]string) map[string]string {
	headers := map[string]string{}
	for name, values := range signed {
		if len(values) == 0 || strings.EqualFold(name, "host") || strings.EqualFold(name, "content-length") {
			continue
		}
		headers[name] = values[0]
	}
	return headers
}
