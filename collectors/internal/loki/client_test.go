package loki_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/Matt-LiFi/alph-e/collectors/internal/loki"
)

func TestClient_QueryRange_OK(t *testing.T) {
	t.Parallel()

	start := time.Date(2024, 1, 1, 0, 0, 0, 0, time.UTC)
	end := start.Add(5 * time.Minute)

	// Minimal Loki response fixture.
	fixture := map[string]any{
		"data": map[string]any{
			"resultType": "streams",
			"result": []any{
				map[string]any{
					"stream": map[string]string{
						"namespace": "demo",
						"app":       "leaky-service",
					},
					"values": []any{
						[]any{"1704067200000000000", "OOMKilled container leaky-service"},
						[]any{"1704067260000000000", "fatal: out of memory"},
					},
				},
			},
		},
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		assert.Equal(t, "/loki/api/v1/query_range", r.URL.Path)
		assert.Equal(t, http.MethodGet, r.Method)

		q := r.URL.Query()
		assert.NotEmpty(t, q.Get("query"))
		assert.NotEmpty(t, q.Get("start"))
		assert.NotEmpty(t, q.Get("end"))
		assert.Equal(t, "50", q.Get("limit"))
		assert.Equal(t, "forward", q.Get("direction"))

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(fixture)
	}))
	defer srv.Close()

	client := loki.NewClient(srv.URL, 5*time.Second)
	streams, err := client.QueryRange(context.Background(), `{namespace="demo"}`, start, end, 50)
	require.NoError(t, err)
	require.Len(t, streams, 1)
	assert.Equal(t, "demo", streams[0].Labels["namespace"])
	assert.Len(t, streams[0].Values, 2)
	assert.Equal(t, "OOMKilled container leaky-service", streams[0].Values[0][1])
}

func TestClient_QueryRange_NonOKStatus(t *testing.T) {
	t.Parallel()

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"message":"parse error"}`))
	}))
	defer srv.Close()

	client := loki.NewClient(srv.URL, 5*time.Second)
	start := time.Now().Add(-time.Hour)
	end := time.Now()
	_, err := client.QueryRange(context.Background(), `bad logql`, start, end, 10)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "unexpected status 400")
}

func TestClient_QueryRange_MalformedJSON(t *testing.T) {
	t.Parallel()

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`not-json`))
	}))
	defer srv.Close()

	client := loki.NewClient(srv.URL, 5*time.Second)
	start := time.Now().Add(-time.Hour)
	end := time.Now()
	_, err := client.QueryRange(context.Background(), `{namespace="demo"}`, start, end, 10)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "decode response")
}

func TestClient_QueryRange_EmptyResult(t *testing.T) {
	t.Parallel()

	fixture := map[string]any{
		"data": map[string]any{
			"resultType": "streams",
			"result":     []any{},
		},
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(fixture)
	}))
	defer srv.Close()

	client := loki.NewClient(srv.URL, 5*time.Second)
	start := time.Now().Add(-time.Hour)
	end := time.Now()
	streams, err := client.QueryRange(context.Background(), `{namespace="demo"}`, start, end, 10)
	require.NoError(t, err)
	assert.Empty(t, streams)
}
