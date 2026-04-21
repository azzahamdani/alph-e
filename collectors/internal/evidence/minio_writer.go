package evidence

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	s3types "github.com/aws/aws-sdk-go-v2/service/s3/types"

	"github.com/Matt-LiFi/alph-e/collectors/internal/contract"
)

// s3API is the minimal subset of the S3 client used by MinioWriter.
// It is extracted so tests can substitute a mock without network.
type s3API interface {
	PutObject(ctx context.Context, params *s3.PutObjectInput, optFns ...func(*s3.Options)) (*s3.PutObjectOutput, error)
}

// MinioConfig holds the connection parameters for the MinIO-compatible
// S3 endpoint. Values are read from environment variables by
// MinioConfigFromEnv.
type MinioConfig struct {
	Endpoint  string // EVIDENCE_S3_ENDPOINT
	AccessKey string // EVIDENCE_S3_ACCESS_KEY
	SecretKey string // EVIDENCE_S3_SECRET_KEY
	Bucket    string // EVIDENCE_S3_BUCKET
	Region    string // EVIDENCE_S3_REGION (default: us-east-1)
}

// MinioConfigFromEnv reads MinioConfig from environment variables.
// Returns an error if any required variable is absent.
func MinioConfigFromEnv() (MinioConfig, error) {
	cfg := MinioConfig{
		Endpoint:  os.Getenv("EVIDENCE_S3_ENDPOINT"),
		AccessKey: os.Getenv("EVIDENCE_S3_ACCESS_KEY"),
		SecretKey: os.Getenv("EVIDENCE_S3_SECRET_KEY"),
		Bucket:    os.Getenv("EVIDENCE_S3_BUCKET"),
		Region:    os.Getenv("EVIDENCE_S3_REGION"),
	}
	if cfg.Region == "" {
		cfg.Region = "us-east-1"
	}
	switch {
	case cfg.Endpoint == "":
		return MinioConfig{}, fmt.Errorf("EVIDENCE_S3_ENDPOINT is required")
	case cfg.AccessKey == "":
		return MinioConfig{}, fmt.Errorf("EVIDENCE_S3_ACCESS_KEY is required")
	case cfg.SecretKey == "":
		return MinioConfig{}, fmt.Errorf("EVIDENCE_S3_SECRET_KEY is required")
	case cfg.Bucket == "":
		return MinioConfig{}, fmt.Errorf("EVIDENCE_S3_BUCKET is required")
	}
	return cfg, nil
}

// BucketNotFoundError is returned by Put when the target bucket does not exist.
// Bucket bootstrap is the Helm chart's responsibility — the writer never
// creates buckets.
type BucketNotFoundError struct {
	Bucket string
}

func (e *BucketNotFoundError) Error() string {
	return fmt.Sprintf("evidence bucket %q does not exist; create it via the Helm chart", e.Bucket)
}

// MinioWriter implements Writer against a MinIO-compatible S3 endpoint.
type MinioWriter struct {
	client s3API
	bucket string
	clock  func() time.Time
}

// NewMinioWriterFromClient constructs a MinioWriter from an already-built
// s3API implementation. Intended for tests that inject a mock client.
func NewMinioWriterFromClient(client s3API, bucket string, clock func() time.Time) (*MinioWriter, error) {
	if client == nil {
		return nil, fmt.Errorf("s3 client must not be nil")
	}
	if bucket == "" {
		return nil, fmt.Errorf("bucket must not be empty")
	}
	if clock == nil {
		clock = time.Now
	}
	return &MinioWriter{client: client, bucket: bucket, clock: clock}, nil
}

// NewMinioWriter constructs a MinioWriter and verifies reachability of the
// configured endpoint. It does NOT create the bucket.
func NewMinioWriter(ctx context.Context, cfg MinioConfig) (*MinioWriter, error) {
	creds := credentials.NewStaticCredentialsProvider(cfg.AccessKey, cfg.SecretKey, "")

	awsCfg, err := config.LoadDefaultConfig(ctx,
		config.WithRegion(cfg.Region),
		config.WithCredentialsProvider(creds),
	)
	if err != nil {
		return nil, fmt.Errorf("load AWS config: %w", err)
	}

	client := s3.NewFromConfig(awsCfg, func(o *s3.Options) {
		o.BaseEndpoint = aws.String(cfg.Endpoint)
		// MinIO requires path-style addressing.
		o.UsePathStyle = true
	})

	return &MinioWriter{
		client: client,
		bucket: cfg.Bucket,
		clock:  time.Now,
	}, nil
}

// Put writes payload to MinIO and returns a populated EvidenceRef.
// It returns *BucketNotFoundError if the target bucket does not exist.
func (w *MinioWriter) Put(ctx context.Context, in PutRequest) (contract.EvidenceRef, error) {
	if in.IncidentID == "" {
		return contract.EvidenceRef{}, fmt.Errorf("incident_id is required")
	}
	if in.ContentType == "" {
		return contract.EvidenceRef{}, fmt.Errorf("content_type is required")
	}

	id := NewEvidenceID()

	_, err := w.client.PutObject(ctx, &s3.PutObjectInput{
		Bucket:        aws.String(w.bucket),
		Key:           aws.String(id),
		Body:          bytes.NewReader(in.Payload),
		ContentType:   aws.String(in.ContentType),
		ContentLength: aws.Int64(int64(len(in.Payload))),
	})
	if err != nil {
		var noSuchBucket *s3types.NoSuchBucket
		if errors.As(err, &noSuchBucket) {
			return contract.EvidenceRef{}, &BucketNotFoundError{Bucket: w.bucket}
		}
		return contract.EvidenceRef{}, fmt.Errorf("s3 PutObject: %w", err)
	}

	return contract.EvidenceRef{
		EvidenceID:  id,
		StorageURI:  fmt.Sprintf("s3://%s/%s", w.bucket, id),
		ContentType: in.ContentType,
		SizeBytes:   int64(len(in.Payload)),
		ExpiresAt:   w.clock().UTC().Add(DefaultTTL),
	}, nil
}
