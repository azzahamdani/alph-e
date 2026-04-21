package prom_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/Matt-LiFi/alph-e/collectors/internal/prom"
)

// fakePrometheus returns an httptest.Server that always responds with body.
func fakePrometheus(t *testing.T, body string, statusCode int) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(statusCode)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return srv
}

const vectorResponse = `{
  "status": "success",
  "data": {
    "resultType": "vector",
    "result": [
      {
        "metric": {"__name__": "up", "job": "prometheus", "namespace": "demo"},
        "value": [1713700000, "42.5"]
      }
    ]
  }
}`

const emptyVectorResponse = `{
  "status": "success",
  "data": {
    "resultType": "vector",
    "result": []
  }
}`

const matrixResponse = `{
  "status": "success",
  "data": {
    "resultType": "matrix",
    "result": [
      {
        "metric": {"__name__": "container_memory_working_set_bytes", "namespace": "demo"},
        "values": [
          [1713700000, "104857600"],
          [1713700060, "120000000"],
          [1713700120, "134217728"]
        ]
      }
    ]
  }
}`

const errorResponse = `{
  "status": "error",
  "errorType": "bad_data",
  "error": "invalid parameter 'query': 1:3: parse error"
}`

func TestQueryInstant_Success(t *testing.T) {
	t.Parallel()
	srv := fakePrometheus(t, vectorResponse, http.StatusOK)
	client := prom.NewClient(srv.URL, nil)

	at := time.Unix(1713700000, 0).UTC()
	res, err := client.QueryInstant(context.Background(), `up{job="prometheus"}`, at)
	require.NoError(t, err)
	assert.NotNil(t, res.Metric)
	assert.InDelta(t, 42.5, res.Value.Value, 0.001)
	assert.Equal(t, at, res.Value.Timestamp)
	assert.NotEmpty(t, res.RawBody)
}

func TestQueryInstant_EmptyResult(t *testing.T) {
	t.Parallel()
	srv := fakePrometheus(t, emptyVectorResponse, http.StatusOK)
	client := prom.NewClient(srv.URL, nil)

	at := time.Unix(1713700000, 0).UTC()
	res, err := client.QueryInstant(context.Background(), `up{job="nope"}`, at)
	require.NoError(t, err)
	assert.Nil(t, res.Metric)
	assert.NotEmpty(t, res.RawBody)
}

func TestQueryInstant_ZeroTimeForbidden(t *testing.T) {
	t.Parallel()
	client := prom.NewClient("http://localhost:9090", nil)
	_, err := client.QueryInstant(context.Background(), `up`, time.Time{})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "must not be zero")
}

func TestQueryRange_Success(t *testing.T) {
	t.Parallel()
	srv := fakePrometheus(t, matrixResponse, http.StatusOK)
	client := prom.NewClient(srv.URL, nil)

	start := time.Unix(1713700000, 0).UTC()
	end := start.Add(5 * time.Minute)
	res, err := client.QueryRange(context.Background(), `container_memory_working_set_bytes`, start, end, 60*time.Second)
	require.NoError(t, err)
	require.Len(t, res.Series, 1)
	assert.Len(t, res.Series[0].Samples, 3)
	assert.InDelta(t, 104857600.0, res.Series[0].Samples[0].Value, 1)
	assert.NotEmpty(t, res.RawBody)
}

func TestQueryRange_ZeroTimeForbidden(t *testing.T) {
	t.Parallel()
	client := prom.NewClient("http://localhost:9090", nil)
	start := time.Unix(1713700000, 0).UTC()
	_, err := client.QueryRange(context.Background(), `up`, start, time.Time{}, 60*time.Second)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "must not be zero")
}

func TestQueryRange_EndBeforeStart(t *testing.T) {
	t.Parallel()
	client := prom.NewClient("http://localhost:9090", nil)
	start := time.Unix(1713700000, 0).UTC()
	end := start.Add(-1 * time.Minute)
	_, err := client.QueryRange(context.Background(), `up`, start, end, 60*time.Second)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "end must be after start")
}

func TestQueryInstant_PrometheusError(t *testing.T) {
	t.Parallel()
	srv := fakePrometheus(t, errorResponse, http.StatusBadRequest)
	client := prom.NewClient(srv.URL, nil)

	at := time.Unix(1713700000, 0).UTC()
	_, err := client.QueryInstant(context.Background(), `bad query!!!`, at)
	require.Error(t, err)
	// Prometheus error should be surfaced verbatim.
	assert.Contains(t, err.Error(), "bad_data")
}

func TestQueryInstant_HTTPError(t *testing.T) {
	t.Parallel()
	srv := fakePrometheus(t, `internal error`, http.StatusInternalServerError)
	client := prom.NewClient(srv.URL, nil)

	at := time.Unix(1713700000, 0).UTC()
	_, err := client.QueryInstant(context.Background(), `up`, at)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "500")
}
