package evidence_test

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/Matt-LiFi/alph-e/collectors/internal/evidence"
)

func TestNullWriter_Put_Defaults(t *testing.T) {
	t.Parallel()

	fixed := time.Date(2026, time.April, 21, 14, 0, 0, 0, time.UTC)
	w := evidence.NullWriter{Clock: func() time.Time { return fixed }}

	ref, err := w.Put(context.Background(), evidence.PutRequest{
		IncidentID:  "inc_1",
		ContentType: "application/x-ndjson",
		Payload:     []byte("hello"),
	})
	require.NoError(t, err)
	assert.Contains(t, ref.StorageURI, "s3://incidents/")
	assert.Equal(t, int64(5), ref.SizeBytes)
	assert.Equal(t, fixed.Add(evidence.DefaultTTL), ref.ExpiresAt)
}

func TestNullWriter_Put_RejectsMissingIncidentID(t *testing.T) {
	t.Parallel()
	_, err := evidence.NullWriter{}.Put(context.Background(), evidence.PutRequest{
		ContentType: "text/plain",
	})
	require.Error(t, err)
}
