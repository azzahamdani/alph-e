package prom_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/Matt-LiFi/alph-e/collectors/internal/contract"
	"github.com/Matt-LiFi/alph-e/collectors/internal/prom"
)

func makeTimeRange() contract.TimeRange {
	start := time.Unix(1713700000, 0).UTC()
	return contract.TimeRange{Start: start, End: start.Add(10 * time.Minute)}
}

func makeInput(question string, scope []string) *contract.CollectorInput {
	tr := makeTimeRange()
	return &contract.CollectorInput{
		IncidentID:             "inc_test",
		Question:               question,
		HypothesisID:           "hyp_test",
		TimeRange:              tr,
		ScopeServices:          scope,
		EnvironmentFingerprint: contract.EnvironmentFingerprint{Cluster: "demo"},
		MaxInternalIterations:  3,
	}
}

// routingServer returns a server that responds with the given body for any path.
func routingServer(t *testing.T, body string) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestParseQuestion_RatePattern(t *testing.T) {
	t.Parallel()
	in := makeInput("metric:rate container_memory_working_set_bytes > 100000000", []string{"demo"})
	pq := prom.ParseQuestion(in)
	assert.Equal(t, prom.KindRate, pq.Kind)
	assert.Contains(t, pq.Expr, "container_memory_working_set_bytes")
	assert.Equal(t, ">", pq.Comparator)
	assert.InDelta(t, 100000000.0, pq.Threshold, 1)
}

func TestParseQuestion_ValuePattern(t *testing.T) {
	t.Parallel()
	in := makeInput("metric:value kube_pod_info", []string{"demo"})
	pq := prom.ParseQuestion(in)
	assert.Equal(t, prom.KindValue, pq.Kind)
	assert.Contains(t, pq.Expr, "kube_pod_info")
}

func TestParseQuestion_Default(t *testing.T) {
	t.Parallel()
	in := makeInput("why is the pod crashing?", []string{"demo"})
	pq := prom.ParseQuestion(in)
	assert.Equal(t, prom.KindDefault, pq.Kind)
	assert.Contains(t, pq.Expr, "kube_pod_container_status_last_terminated_reason")
}

func TestParseQuestion_DefaultNamespace(t *testing.T) {
	t.Parallel()
	// No scope services → namespace defaults to "demo".
	in := makeInput("why is the pod crashing?", nil)
	pq := prom.ParseQuestion(in)
	assert.Contains(t, pq.Expr, `"demo"`)
}

func TestDispatch_RateQuery_AboveThreshold(t *testing.T) {
	t.Parallel()
	// All three samples are above 100MB threshold → confidence = 1.0
	srv := routingServer(t, matrixResponse)
	client := prom.NewClient(srv.URL, nil)
	in := makeInput("metric:rate container_memory_working_set_bytes > 100000000", []string{"demo"})

	dr := prom.Dispatch(context.Background(), client, in, 3)
	require.NotEmpty(t, dr.Summary)
	assert.Greater(t, dr.Confidence, 0.0)
	assert.Equal(t, 1, dr.QueryCount)
	assert.NotEmpty(t, dr.RawBody)
}

func TestDispatch_ValueQuery(t *testing.T) {
	t.Parallel()
	srv := routingServer(t, vectorResponse)
	client := prom.NewClient(srv.URL, nil)
	in := makeInput("metric:value up", []string{"demo"})

	dr := prom.Dispatch(context.Background(), client, in, 3)
	require.NotEmpty(t, dr.Summary)
	assert.Equal(t, 1.0, dr.Confidence) // value > 0 → confidence 1.0
	assert.Equal(t, 1, dr.QueryCount)
}

func TestDispatch_DefaultQuery_NoData(t *testing.T) {
	t.Parallel()
	srv := routingServer(t, emptyVectorResponse)
	client := prom.NewClient(srv.URL, nil)
	in := makeInput("why is the pod crashing?", []string{"demo"})

	dr := prom.Dispatch(context.Background(), client, in, 3)
	assert.Equal(t, 0.0, dr.Confidence)
	assert.Contains(t, dr.Summary, "no OOM termination data")
}

func TestDispatch_PrometheusError_SurfacedVerbatim(t *testing.T) {
	t.Parallel()
	// Prometheus returns an error — it must appear in the summary.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(errorResponse))
	}))
	t.Cleanup(srv.Close)

	client := prom.NewClient(srv.URL, nil)
	in := makeInput("why is the pod crashing?", []string{"demo"})

	dr := prom.Dispatch(context.Background(), client, in, 3)
	assert.Equal(t, 0.0, dr.Confidence)
	assert.Contains(t, dr.Summary, "prometheus query error")
}

func TestDispatch_ContextCancelled(t *testing.T) {
	t.Parallel()
	// A server that hangs until the client gives up.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	t.Cleanup(srv.Close)

	client := prom.NewClient(srv.URL, &http.Client{Timeout: 200 * time.Millisecond})
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	in := makeInput("metric:value up", []string{"demo"})
	dr := prom.Dispatch(ctx, client, in, 1)
	assert.Equal(t, 0.0, dr.Confidence)
	assert.Contains(t, dr.Summary, "prometheus query error")
}
