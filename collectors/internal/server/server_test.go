package server_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/Matt-LiFi/alph-e/collectors/internal/contract"
	"github.com/Matt-LiFi/alph-e/collectors/internal/evidence"
	"github.com/Matt-LiFi/alph-e/collectors/internal/server"
)

type stubCollector struct{}

func (stubCollector) Name() string { return "stub" }
func (stubCollector) Collect(_ context.Context, in contract.CollectorInput) (contract.CollectorOutput, error) {
	ref, _ := evidence.NullWriter{}.Put(context.Background(), evidence.PutRequest{
		IncidentID:  in.IncidentID,
		ContentType: "text/plain",
		Payload:     []byte("stub"),
	})
	return contract.CollectorOutput{
		Finding: contract.Finding{
			ID:            "f_stub",
			CollectorName: "stub",
			Question:      in.Question,
			Summary:       fmt.Sprintf("stubbed %s", in.IncidentID),
			EvidenceID:    ref.EvidenceID,
			Confidence:    0.5,
			CreatedAt:     time.Now().UTC(),
		},
		Evidence: ref,
	}, nil
}

func validInput() contract.CollectorInput {
	start := time.Date(2026, time.April, 21, 14, 0, 0, 0, time.UTC)
	return contract.CollectorInput{
		IncidentID:             "inc_1",
		Question:               "q",
		HypothesisID:           "hyp_1",
		TimeRange:              contract.TimeRange{Start: start, End: start.Add(15 * time.Minute)},
		ScopeServices:          []string{"leaky-service"},
		EnvironmentFingerprint: contract.EnvironmentFingerprint{Cluster: "demo"},
	}
}

func TestServer_CollectOK(t *testing.T) {
	t.Parallel()
	s := &server.Server{Collector: stubCollector{}}
	srv := httptest.NewServer(s.Handler())
	defer srv.Close()

	body, _ := json.Marshal(validInput())
	res, err := http.Post(srv.URL+"/collect", "application/json", bytes.NewReader(body))
	require.NoError(t, err)
	defer res.Body.Close()
	raw, _ := io.ReadAll(res.Body)
	require.Equalf(t, http.StatusOK, res.StatusCode, "body=%s", string(raw))

	var out contract.CollectorOutput
	require.NoError(t, json.Unmarshal(raw, &out))
	assert.NotEmpty(t, out.Finding.Summary)
	assert.Equal(t, out.Finding.EvidenceID, out.Evidence.EvidenceID)
}

func TestServer_RejectsBadInput(t *testing.T) {
	t.Parallel()
	s := &server.Server{Collector: stubCollector{}}
	srv := httptest.NewServer(s.Handler())
	defer srv.Close()

	res, err := http.Post(srv.URL+"/collect", "application/json", bytes.NewReader([]byte(`{}`)))
	require.NoError(t, err)
	defer res.Body.Close()
	assert.Equal(t, http.StatusBadRequest, res.StatusCode)
}

func TestServer_Healthz(t *testing.T) {
	t.Parallel()
	s := &server.Server{Collector: stubCollector{}}
	srv := httptest.NewServer(s.Handler())
	defer srv.Close()

	res, err := http.Get(srv.URL + "/healthz")
	require.NoError(t, err)
	defer res.Body.Close()
	assert.Equal(t, http.StatusOK, res.StatusCode)
}
