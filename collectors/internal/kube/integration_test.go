//go:build integration

package kube_test

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/Matt-LiFi/alph-e/collectors/internal/contract"
	"github.com/Matt-LiFi/alph-e/collectors/internal/kube"
)

// TestIntegration_PodStatus_DemoNamespace hits the lab k3d cluster via whatever
// kubeconfig KUBECONFIG_AGENT / KUBECONFIG / ~/.kube/config resolves to.
//
// Prerequisites:
//
//	k3d cluster running (task up)
//	leaky-service deployed in the demo namespace (task demo:deploy)
func TestIntegration_PodStatus_DemoNamespace(t *testing.T) {
	cs, err := kube.NewClientset()
	require.NoError(t, err, "NewClientset must succeed against the lab cluster")

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	in := contract.CollectorInput{
		IncidentID:             "inc_integration_kube",
		Question:               "pod:status demo",
		HypothesisID:           "hyp_integration_kube",
		TimeRange:              contract.TimeRange{Start: time.Now().Add(-15 * time.Minute), End: time.Now()},
		ScopeServices:          []string{"demo"},
		EnvironmentFingerprint: contract.EnvironmentFingerprint{Cluster: "k3d-lab"},
		MaxInternalIterations:  3,
	}

	dr := kube.Dispatch(ctx, cs, in)
	t.Logf("summary: %s", dr.Summary)
	t.Logf("confidence: %f", dr.Confidence)
	t.Logf("raw body length: %d bytes", len(dr.RawBody))

	require.NotEmpty(t, dr.Summary)
	// The demo namespace must have at least one pod (leaky-service).
	assert.Greater(t, dr.Confidence, 0.0)
	assert.Contains(t, dr.Summary, "pod(s)")
}

// TestIntegration_TerminatedPods_OOMKilled checks that at least one OOMKilled
// termination is visible in the demo namespace (the leaky-service OOM loop).
func TestIntegration_TerminatedPods_OOMKilled(t *testing.T) {
	cs, err := kube.NewClientset()
	require.NoError(t, err, "NewClientset must succeed against the lab cluster")

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	in := contract.CollectorInput{
		IncidentID:             "inc_integration_kube_term",
		Question:               "why are pods crashing in demo?",
		HypothesisID:           "hyp_integration_kube_term",
		TimeRange:              contract.TimeRange{Start: time.Now().Add(-15 * time.Minute), End: time.Now()},
		ScopeServices:          []string{"demo"},
		EnvironmentFingerprint: contract.EnvironmentFingerprint{Cluster: "k3d-lab"},
		MaxInternalIterations:  3,
	}

	dr := kube.Dispatch(ctx, cs, in)
	t.Logf("summary: %s", dr.Summary)
	t.Logf("confidence: %f", dr.Confidence)

	require.NotEmpty(t, dr.Summary)

	// Expect at least one OOMKilled termination — the leaky-service OOM loop
	// means lastState.terminated.reason should be "OOMKilled" on at least one
	// container.  This is a soft check; a freshly started cluster may not have
	// cycled yet.
	if !assert.Contains(t, dr.Summary, "OOMKilled") {
		t.Logf("WARN: no OOMKilled termination found -- cluster may not have cycled yet")
		t.Logf("Run: task demo:watch && sleep 120 && go test -tags=integration ./internal/kube/...")
	}
}

// TestIntegration_PodEvents_LeakyPod fetches events for the first leaky-service
// pod found in the demo namespace.
func TestIntegration_PodEvents_LeakyPod(t *testing.T) {
	cs, err := kube.NewClientset()
	require.NoError(t, err)

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	// First list pods to get a real pod name.
	pods, listErr := cs.CoreV1().Pods("demo").List(ctx, metav1.ListOptions{})
	require.NoError(t, listErr)
	require.NotEmpty(t, pods.Items, "demo namespace must have at least one pod")

	podName := pods.Items[0].Name
	t.Logf("checking events for pod demo/%s", podName)

	in := contract.CollectorInput{
		IncidentID:             "inc_integration_kube_events",
		Question:               "pod:events demo/" + podName,
		HypothesisID:           "hyp_integration_kube_events",
		TimeRange:              contract.TimeRange{Start: time.Now().Add(-15 * time.Minute), End: time.Now()},
		ScopeServices:          []string{"demo"},
		EnvironmentFingerprint: contract.EnvironmentFingerprint{Cluster: "k3d-lab"},
		MaxInternalIterations:  3,
	}

	dr := kube.Dispatch(ctx, cs, in)
	t.Logf("summary: %s", dr.Summary)

	require.NotEmpty(t, dr.Summary)
	// Events might be empty if the pod was just created; both outcomes are valid.
	if dr.Summary == "no events found for pod demo/"+podName {
		t.Logf("INFO: no events yet for pod %s -- acceptable on a freshly scheduled pod", podName)
	} else {
		assert.Contains(t, dr.Summary, "event(s)")
	}
}
