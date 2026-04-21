package loki_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/Matt-LiFi/alph-e/collectors/internal/loki"
)

// lokiFixture builds a minimal Loki response with n lines.
func lokiFixture(n int) map[string]any {
	values := make([]any, n)
	baseNs := int64(1704067200000000000)
	for i := 0; i < n; i++ {
		values[i] = []any{
			jsonTS(baseNs + int64(i)*1_000_000_000),
			"error: something went wrong",
		}
	}
	return map[string]any{
		"data": map[string]any{
			"resultType": "streams",
			"result": []any{
				map[string]any{
					"stream": map[string]string{"namespace": "demo", "app": "leaky-service"},
					"values": values,
				},
			},
		},
	}
}

// jsonTS renders a nanosecond epoch as the plain integer string Loki uses.
func jsonTS(ns int64) string {
	b, _ := json.Marshal(ns)
	return string(b)
}

func newTestServer(t *testing.T, fixture map[string]any, captureQuery *string) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if captureQuery != nil {
			*captureQuery = r.URL.Query().Get("query")
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(fixture)
	}))
}

// --- LogQL builder tests ---

func TestBuildLogQL_Default(t *testing.T) {
	t.Parallel()

	var captured string
	srv := newTestServer(t, lokiFixture(1), &captured)
	defer srv.Close()

	client := loki.NewClient(srv.URL, 5*time.Second)
	req := loki.DispatchRequest{
		Question:  "what is happening",
		Namespace: "demo",
		Start:     time.Now().Add(-time.Hour),
		End:       time.Now(),
		MaxLines:  100,
	}
	_, err := loki.Dispatch(context.Background(), client, &req)
	require.NoError(t, err)
	assert.Contains(t, captured, `namespace="demo"`)
	assert.Contains(t, captured, `(?i)error|fatal|panic|oom`)
}

func TestBuildLogQL_Contains(t *testing.T) {
	t.Parallel()

	var captured string
	srv := newTestServer(t, lokiFixture(0), &captured)
	defer srv.Close()

	client := loki.NewClient(srv.URL, 5*time.Second)
	req := loki.DispatchRequest{
		Question:  "log:contains OOMKilled",
		Namespace: "demo",
		Start:     time.Now().Add(-time.Hour),
		End:       time.Now(),
		MaxLines:  100,
	}
	_, err := loki.Dispatch(context.Background(), client, &req)
	require.NoError(t, err)
	assert.Contains(t, captured, `|= "OOMKilled"`)
}

func TestBuildLogQL_Rate(t *testing.T) {
	t.Parallel()

	var captured string
	// A rate query returns a matrix, not streams; but for dispatch testing we
	// still return a streams fixture (the evidence writer just gets empty output).
	srv := newTestServer(t, lokiFixture(0), &captured)
	defer srv.Close()

	client := loki.NewClient(srv.URL, 5*time.Second)
	req := loki.DispatchRequest{
		Question:  "log:rate error",
		Namespace: "demo",
		Start:     time.Now().Add(-time.Hour),
		End:       time.Now(),
		MaxLines:  100,
	}
	_, err := loki.Dispatch(context.Background(), client, &req)
	require.NoError(t, err)
	assert.Contains(t, captured, `sum(rate(`)
	assert.Contains(t, captured, `[5m]))`)
	assert.Contains(t, captured, `|~ "error"`)
}

func TestBuildLogQL_ScopeServices(t *testing.T) {
	t.Parallel()

	var captured string
	srv := newTestServer(t, lokiFixture(0), &captured)
	defer srv.Close()

	client := loki.NewClient(srv.URL, 5*time.Second)
	req := loki.DispatchRequest{
		Question:      "what errors",
		Namespace:     "demo",
		ScopeServices: []string{"leaky-service", "api-gateway"},
		Start:         time.Now().Add(-time.Hour),
		End:           time.Now(),
		MaxLines:      100,
	}
	_, err := loki.Dispatch(context.Background(), client, &req)
	require.NoError(t, err)
	assert.Contains(t, captured, `app=~"leaky-service|api-gateway"`)
}

// --- NDJSON output tests ---

func TestDispatch_NDJSONOutput(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t, lokiFixture(3), nil)
	defer srv.Close()

	client := loki.NewClient(srv.URL, 5*time.Second)
	req := loki.DispatchRequest{
		Question:  "what errors",
		Namespace: "demo",
		Start:     time.Now().Add(-time.Hour),
		End:       time.Now(),
		MaxLines:  100,
	}
	result, err := loki.Dispatch(context.Background(), client, &req)
	require.NoError(t, err)
	assert.Equal(t, 3, result.MatchCount)

	lines := strings.Split(strings.TrimSpace(string(result.NDJSON)), "\n")
	require.Len(t, lines, 3)
	for _, line := range lines {
		var rec map[string]any
		require.NoError(t, json.Unmarshal([]byte(line), &rec), "each line must be valid JSON")
		assert.Contains(t, rec, "ts_ns")
		assert.Contains(t, rec, "line")
		assert.Contains(t, rec, "labels")
	}
}

func TestDispatch_MaxLinesCapEnforced(t *testing.T) {
	t.Parallel()

	// Server returns 20 lines; cap is 5.
	srv := newTestServer(t, lokiFixture(20), nil)
	defer srv.Close()

	client := loki.NewClient(srv.URL, 5*time.Second)
	req := loki.DispatchRequest{
		Question:  "what errors",
		Namespace: "demo",
		Start:     time.Now().Add(-time.Hour),
		End:       time.Now(),
		MaxLines:  5,
	}
	result, err := loki.Dispatch(context.Background(), client, &req)
	require.NoError(t, err)
	assert.Equal(t, 5, result.MatchCount)
	lines := strings.Split(strings.TrimSpace(string(result.NDJSON)), "\n")
	assert.Len(t, lines, 5)
}

func TestDispatch_EmptyResult(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t, lokiFixture(0), nil)
	defer srv.Close()

	client := loki.NewClient(srv.URL, 5*time.Second)
	req := loki.DispatchRequest{
		Question:  "what errors",
		Namespace: "demo",
		Start:     time.Now().Add(-time.Hour),
		End:       time.Now(),
		MaxLines:  100,
	}
	result, err := loki.Dispatch(context.Background(), client, &req)
	require.NoError(t, err)
	assert.Equal(t, 0, result.MatchCount)
	assert.Empty(t, result.NDJSON)
}

// --- Validation tests ---

func TestDispatch_InvalidMaxLines(t *testing.T) {
	t.Parallel()

	client := loki.NewClient("http://unused", 5*time.Second)
	req := loki.DispatchRequest{
		Question:  "anything",
		Namespace: "demo",
		Start:     time.Now().Add(-time.Hour),
		End:       time.Now(),
		MaxLines:  0,
	}
	_, err := loki.Dispatch(context.Background(), client, &req)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "MaxLines must be > 0")
}

func TestDispatch_EndBeforeStart(t *testing.T) {
	t.Parallel()

	client := loki.NewClient("http://unused", 5*time.Second)
	now := time.Now()
	req := loki.DispatchRequest{
		Question:  "anything",
		Namespace: "demo",
		Start:     now,
		End:       now.Add(-time.Minute),
		MaxLines:  10,
	}
	_, err := loki.Dispatch(context.Background(), client, &req)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "End must be after Start")
}
