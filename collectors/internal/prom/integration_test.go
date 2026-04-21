//go:build integration

package prom_test

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/Matt-LiFi/alph-e/collectors/internal/contract"
	"github.com/Matt-LiFi/alph-e/collectors/internal/prom"
)

// TestIntegration_DefaultQuery_OOMData hits the lab Prometheus via port-forward.
//
// Prerequisites:
//
//	kubectl port-forward -n monitoring svc/kps-kube-prometheus-stack-prometheus 9090:9090
//
// The test expects at least one kube_pod_container_status_last_terminated_reason
// sample in the demo namespace (the leaky-service OOM loop).
func TestIntegration_DefaultQuery_OOMData(t *testing.T) {
	promURL := os.Getenv("PROMETHEUS_URL")
	if promURL == "" {
		promURL = "http://localhost:9090"
	}

	client := prom.NewClient(promURL, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	now := time.Now().UTC()
	tr := contract.TimeRange{Start: now.Add(-15 * time.Minute), End: now}

	in := &contract.CollectorInput{
		IncidentID:             "inc_integration",
		Question:               "why is the pod crashing?",
		HypothesisID:           "hyp_integration",
		TimeRange:              tr,
		ScopeServices:          []string{"demo"},
		EnvironmentFingerprint: contract.EnvironmentFingerprint{Cluster: "k3d-lab"},
		MaxInternalIterations:  3,
	}

	dr := prom.Dispatch(ctx, client, in, 3)
	t.Logf("summary: %s", dr.Summary)
	t.Logf("confidence: %f", dr.Confidence)
	t.Logf("raw body length: %d bytes", len(dr.RawBody))

	require.NotEmpty(t, dr.Summary, "summary must not be empty")
	// The leaky-service OOM loop should have produced at least one termination.
	assert.Greater(t, dr.Confidence, 0.0, "expected non-zero confidence when OOM loop is running")
}

// TestIntegration_RateQuery_MemoryPressure queries memory bytes over the last
// 15 minutes for the demo namespace.
func TestIntegration_RateQuery_MemoryPressure(t *testing.T) {
	promURL := os.Getenv("PROMETHEUS_URL")
	if promURL == "" {
		promURL = "http://localhost:9090"
	}

	client := prom.NewClient(promURL, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	now := time.Now().UTC()
	tr := contract.TimeRange{Start: now.Add(-15 * time.Minute), End: now}

	in := &contract.CollectorInput{
		IncidentID:             "inc_integration_rate",
		Question:               "metric:rate container_memory_working_set_bytes > 67108864",
		HypothesisID:           "hyp_integration_rate",
		TimeRange:              tr,
		ScopeServices:          []string{"demo"},
		EnvironmentFingerprint: contract.EnvironmentFingerprint{Cluster: "k3d-lab"},
		MaxInternalIterations:  3,
	}

	dr := prom.Dispatch(ctx, client, in, 3)
	t.Logf("summary: %s", dr.Summary)
	t.Logf("confidence: %f", dr.Confidence)

	require.NotEmpty(t, dr.Summary)
	// Memory should be above 64MiB for the leaky pod.
	assert.Greater(t, dr.Confidence, 0.0, "expected memory above 64MiB threshold for leaky-service")
}
