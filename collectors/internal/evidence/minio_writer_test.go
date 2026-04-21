package evidence_test

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/service/s3"
	s3types "github.com/aws/aws-sdk-go-v2/service/s3/types"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/Matt-LiFi/alph-e/collectors/internal/evidence"
)

// --- mock ---

type mockS3Client struct {
	// If putErr is set, PutObject returns it.
	putErr error
	// Capture what was sent.
	lastInput *s3.PutObjectInput
}

func (m *mockS3Client) PutObject(_ context.Context, params *s3.PutObjectInput, _ ...func(*s3.Options)) (*s3.PutObjectOutput, error) {
	m.lastInput = params
	if m.putErr != nil {
		return nil, m.putErr
	}
	return &s3.PutObjectOutput{}, nil
}

// newTestWriter builds a MinioWriter with a mock S3 client and a fixed clock.
func newTestWriter(t *testing.T, mock *mockS3Client, fixedNow time.Time) *evidence.MinioWriter {
	t.Helper()
	w, err := evidence.NewMinioWriterFromClient(mock, "test-bucket", func() time.Time { return fixedNow })
	require.NoError(t, err)
	return w
}

// --- tests ---

func TestMinioWriter_Put_HappyPath(t *testing.T) {
	t.Parallel()

	fixed := time.Date(2026, time.April, 21, 12, 0, 0, 0, time.UTC)
	mock := &mockS3Client{}
	w := newTestWriter(t, mock, fixed)

	ref, err := w.Put(context.Background(), evidence.PutRequest{
		IncidentID:  "inc_abc",
		ContentType: "application/json",
		Payload:     []byte(`{"hello":"world"}`),
	})
	require.NoError(t, err)

	assert.True(t, strings.HasPrefix(ref.StorageURI, "s3://test-bucket/ev_"), "StorageURI should start with s3://test-bucket/ev_")
	assert.Equal(t, int64(17), ref.SizeBytes)
	assert.Equal(t, "application/json", ref.ContentType)
	assert.Equal(t, fixed.Add(evidence.DefaultTTL), ref.ExpiresAt)
	assert.NotEmpty(t, ref.EvidenceID)

	// Key sent to S3 must match the EvidenceID.
	require.NotNil(t, mock.lastInput)
	assert.Equal(t, ref.EvidenceID, *mock.lastInput.Key)
	assert.Equal(t, "test-bucket", *mock.lastInput.Bucket)
}

func TestMinioWriter_Put_RejectsMissingIncidentID(t *testing.T) {
	t.Parallel()

	mock := &mockS3Client{}
	w := newTestWriter(t, mock, time.Now())

	_, err := w.Put(context.Background(), evidence.PutRequest{
		ContentType: "application/json",
		Payload:     []byte("x"),
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "incident_id")
}

func TestMinioWriter_Put_RejectsMissingContentType(t *testing.T) {
	t.Parallel()

	mock := &mockS3Client{}
	w := newTestWriter(t, mock, time.Now())

	_, err := w.Put(context.Background(), evidence.PutRequest{
		IncidentID: "inc_abc",
		Payload:    []byte("x"),
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "content_type")
}

func TestMinioWriter_Put_BucketNotFound(t *testing.T) {
	t.Parallel()

	mock := &mockS3Client{putErr: &s3types.NoSuchBucket{}}
	w := newTestWriter(t, mock, time.Now())

	_, err := w.Put(context.Background(), evidence.PutRequest{
		IncidentID:  "inc_abc",
		ContentType: "application/json",
		Payload:     []byte("x"),
	})
	require.Error(t, err)

	var notFound *evidence.BucketNotFoundError
	assert.ErrorAs(t, err, &notFound, "expected BucketNotFoundError")
	assert.Contains(t, notFound.Error(), "test-bucket")
}

func TestMinioWriter_Put_GenericS3Error(t *testing.T) {
	t.Parallel()

	mock := &mockS3Client{putErr: fmt.Errorf("connection refused")}
	w := newTestWriter(t, mock, time.Now())

	_, err := w.Put(context.Background(), evidence.PutRequest{
		IncidentID:  "inc_abc",
		ContentType: "application/json",
		Payload:     []byte("x"),
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "s3 PutObject")
}

func TestMinioWriter_Put_EmptyPayload(t *testing.T) {
	t.Parallel()

	mock := &mockS3Client{}
	w := newTestWriter(t, mock, time.Now())

	ref, err := w.Put(context.Background(), evidence.PutRequest{
		IncidentID:  "inc_abc",
		ContentType: "application/json",
		Payload:     []byte{},
	})
	require.NoError(t, err)
	assert.Equal(t, int64(0), ref.SizeBytes)
}

func TestMinioConfigFromEnv_Defaults(t *testing.T) {
	t.Setenv("EVIDENCE_S3_ENDPOINT", "http://minio:9000")
	t.Setenv("EVIDENCE_S3_ACCESS_KEY", "minio")
	t.Setenv("EVIDENCE_S3_SECRET_KEY", "minio123")
	t.Setenv("EVIDENCE_S3_BUCKET", "incidents")
	t.Setenv("EVIDENCE_S3_REGION", "")

	cfg, err := evidence.MinioConfigFromEnv()
	require.NoError(t, err)
	assert.Equal(t, "us-east-1", cfg.Region)
}

func TestMinioConfigFromEnv_MissingEndpoint(t *testing.T) {
	t.Setenv("EVIDENCE_S3_ENDPOINT", "")
	t.Setenv("EVIDENCE_S3_ACCESS_KEY", "k")
	t.Setenv("EVIDENCE_S3_SECRET_KEY", "s")
	t.Setenv("EVIDENCE_S3_BUCKET", "b")

	_, err := evidence.MinioConfigFromEnv()
	require.Error(t, err)
	assert.Contains(t, err.Error(), "EVIDENCE_S3_ENDPOINT")
}
