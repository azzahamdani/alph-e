// Package evidence writes collector artefacts to MinIO and returns the
// EvidenceRef the orchestrator embeds in a Finding.
//
// The writer is shaped as an interface so tests can substitute an in-memory
// implementation without spinning MinIO up. Real S3 wiring is deferred to
// WI-005/006/007 — this package provides the seams.
package evidence

import (
	"context"
	"fmt"
	"time"

	"github.com/google/uuid"

	"github.com/Matt-LiFi/alph-e/collectors/internal/contract"
)

// DefaultTTL matches the MinIO lifecycle rule the agent infra creates.
const DefaultTTL = 30 * 24 * time.Hour

// Writer persists a blob and returns the metadata envelope.
type Writer interface {
	Put(ctx context.Context, in PutRequest) (contract.EvidenceRef, error)
}

// PutRequest bundles everything Put needs.
type PutRequest struct {
	IncidentID  string
	ContentType string
	Payload     []byte
}

// NewEvidenceID produces a collision-resistant id; safe to call without a DB.
func NewEvidenceID() string {
	return "ev_" + uuid.New().String()[:12]
}

// NullWriter is a stand-in Writer for local/dev runs. It validates input, mints
// an EvidenceRef, and returns it without any real I/O. Production wiring
// substitutes a MinIO-backed Writer.
type NullWriter struct {
	Bucket string
	TTL    time.Duration
	Clock  func() time.Time
}

// Put implements Writer.
func (w NullWriter) Put(_ context.Context, in PutRequest) (contract.EvidenceRef, error) {
	if in.IncidentID == "" {
		return contract.EvidenceRef{}, fmt.Errorf("incident_id is required")
	}
	if in.ContentType == "" {
		return contract.EvidenceRef{}, fmt.Errorf("content_type is required")
	}
	bucket := w.Bucket
	if bucket == "" {
		bucket = "incidents"
	}
	ttl := w.TTL
	if ttl == 0 {
		ttl = DefaultTTL
	}
	clock := w.Clock
	if clock == nil {
		clock = time.Now
	}
	id := NewEvidenceID()
	return contract.EvidenceRef{
		EvidenceID:  id,
		StorageURI:  fmt.Sprintf("s3://%s/%s", bucket, id),
		ContentType: in.ContentType,
		SizeBytes:   int64(len(in.Payload)),
		ExpiresAt:   clock().UTC().Add(ttl),
	}, nil
}
