package main

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/Matt-LiFi/alph-e/collectors/internal/contract"
	"github.com/Matt-LiFi/alph-e/collectors/internal/evidence"
	"github.com/Matt-LiFi/alph-e/collectors/internal/loki"
	"github.com/Matt-LiFi/alph-e/collectors/internal/server"
)

// lokiFixtureServer spins up an httptest server returning the given number of
// log line matches. Lines contain the word "error" to trigger the default
// LogQL pattern.
func lokiFixtureServer(t *testing.T, nLines int) *httptest.Server {
	t.Helper()
	type lokiResp struct {
		Data struct {
			ResultType string        `json:"resultType"`
			Result     []loki.Stream `json:"result"`
		} `json:"data"`
	}

	values := make([][2]string, nLines)
	baseNs := int64(1704067200000000000)
	for i := 0; i < nLines; i++ {
		values[i] = [2]string{
			jsonInt64(baseNs + int64(i)*1_000_000_000),
			"error: memory usage critical",
		}
	}

	resp := lokiResp{}
	resp.Data.ResultType = "streams"
	if nLines > 0 {
		resp.Data.Result = []loki.Stream{
			{
				Labels: map[string]string{"namespace": "demo", "app": "leaky-service"},
				Values: values,
			},
		}
	}

	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(resp)
	}))
}

func jsonInt64(n int64) string {
	b, _ := json.Marshal(n)
	return string(b)
}

func buildTestCollector(t *testing.T, lokiSrvURL string, maxLines int) *lokiCollector {
	t.Helper()
	return &lokiCollector{
		writer:   evidence.NullWriter{Bucket: "test"},
		client:   loki.NewClient(lokiSrvURL, 5*time.Second),
		maxLines: maxLines,
		logger:   nil,
	}
}

func validInput() contract.CollectorInput {
	now := time.Now().UTC()
	return contract.CollectorInput{
		IncidentID:   "inc_test",
		Question:     "what errors occurred",
		HypothesisID: "hyp_test",
		TimeRange: contract.TimeRange{
			Start: now.Add(-30 * time.Minute),
			End:   now,
		},
		ScopeServices: []string{"leaky-service"},
		EnvironmentFingerprint: contract.EnvironmentFingerprint{
			Cluster: "k3d-local",
		},
		MaxInternalIterations: 3,
	}
}

// --- Unit tests on the Collect method ---

func TestCollect_ReturnsNDJSONEvidence(t *testing.T) {
	t.Parallel()

	lokiSrv := lokiFixtureServer(t, 10)
	defer lokiSrv.Close()

	c := buildTestCollector(t, lokiSrv.URL, 5000)
	out, err := c.Collect(context.Background(), validInput())
	require.NoError(t, err)

	assert.Equal(t, "loki", out.Finding.CollectorName)
	assert.Equal(t, 10, int(out.Finding.Confidence*100)) // 10/100 = 0.10
	assert.NotEmpty(t, out.Finding.Summary)
	assert.NotEmpty(t, out.Finding.EvidenceID)
	assert.Equal(t, "application/x-ndjson", out.Evidence.ContentType)
	assert.Equal(t, 1, out.ToolCallsUsed)
}

func TestCollect_ZeroMatches_LowConfidence(t *testing.T) {
	t.Parallel()

	lokiSrv := lokiFixtureServer(t, 0)
	defer lokiSrv.Close()

	c := buildTestCollector(t, lokiSrv.URL, 5000)
	out, err := c.Collect(context.Background(), validInput())
	require.NoError(t, err)

	assert.Equal(t, 0.0, out.Finding.Confidence)
	assert.Contains(t, out.Finding.SuggestedFollowups[0], "broaden")
}

func TestCollect_ConfidenceCappedAt1(t *testing.T) {
	t.Parallel()

	// 200 matches → min(1.0, 200/100) = 1.0
	lokiSrv := lokiFixtureServer(t, 200)
	defer lokiSrv.Close()

	c := buildTestCollector(t, lokiSrv.URL, 5000)
	out, err := c.Collect(context.Background(), validInput())
	require.NoError(t, err)
	assert.Equal(t, 1.0, out.Finding.Confidence)
}

func TestCollect_MaxLinesRespected(t *testing.T) {
	t.Parallel()

	lokiSrv := lokiFixtureServer(t, 50)
	defer lokiSrv.Close()

	// cap is 10 lines — evidence blob should have exactly 10 JSON lines
	c := buildTestCollector(t, lokiSrv.URL, 10)
	out, err := c.Collect(context.Background(), validInput())
	require.NoError(t, err)
	assert.Contains(t, out.Finding.Summary, "10")
}

// --- HTTP integration via server.Server ---

func TestHTTPCollect_EndToEnd(t *testing.T) {
	t.Parallel()

	lokiSrv := lokiFixtureServer(t, 5)
	defer lokiSrv.Close()

	c := buildTestCollector(t, lokiSrv.URL, 5000)
	srv := &server.Server{Collector: c}
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	body, _ := json.Marshal(validInput())
	resp, err := http.Post(ts.URL+"/collect", "application/json", bytes.NewReader(body)) //nolint:noctx // test helper: context not needed
	require.NoError(t, err)
	defer resp.Body.Close() //nolint:errcheck // best-effort drain in test

	assert.Equal(t, http.StatusOK, resp.StatusCode)

	var out contract.CollectorOutput
	require.NoError(t, json.NewDecoder(resp.Body).Decode(&out))
	assert.Equal(t, "loki", out.Finding.CollectorName)
	assert.NotEmpty(t, out.Finding.EvidenceID)
}

func TestHTTPCollect_InvalidBody_400(t *testing.T) {
	t.Parallel()

	lokiSrv := lokiFixtureServer(t, 0)
	defer lokiSrv.Close()

	c := buildTestCollector(t, lokiSrv.URL, 5000)
	srv := &server.Server{Collector: c}
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	resp, err := http.Post(ts.URL+"/collect", "application/json", bytes.NewReader([]byte(`{bad json`))) //nolint:noctx // test helper: context not needed
	require.NoError(t, err)
	defer resp.Body.Close() //nolint:errcheck // best-effort drain in test
	assert.Equal(t, http.StatusBadRequest, resp.StatusCode)
}

func TestHTTPHealthz(t *testing.T) {
	t.Parallel()

	lokiSrv := lokiFixtureServer(t, 0)
	defer lokiSrv.Close()

	c := buildTestCollector(t, lokiSrv.URL, 5000)
	srv := &server.Server{Collector: c}
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	resp, err := http.Get(ts.URL + "/healthz") //nolint:noctx // test helper: context not needed
	require.NoError(t, err)
	defer resp.Body.Close() //nolint:errcheck // best-effort drain in test
	assert.Equal(t, http.StatusOK, resp.StatusCode)
}

// --- Namespace derivation ---

func TestNamespaceFromInput_ServicePrefix(t *testing.T) {
	t.Parallel()
	in := contract.CollectorInput{ScopeServices: []string{"prod/api-gateway", "prod/frontend"}}
	assert.Equal(t, "prod", namespaceFromInput(in))
}

func TestNamespaceFromInput_NoSlash(t *testing.T) {
	t.Parallel()
	in := contract.CollectorInput{ScopeServices: []string{"leaky-service"}}
	assert.Equal(t, "demo", namespaceFromInput(in))
}

func TestNamespaceFromInput_NoServices(t *testing.T) {
	t.Parallel()
	in := contract.CollectorInput{}
	assert.Equal(t, "demo", namespaceFromInput(in))
}
