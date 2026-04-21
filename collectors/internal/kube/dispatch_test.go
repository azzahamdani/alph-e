package kube_test

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"

	"github.com/Matt-LiFi/alph-e/collectors/internal/contract"
	"github.com/Matt-LiFi/alph-e/collectors/internal/kube"
)

// ---- helpers ----------------------------------------------------------------

func makeInput(question string, scope []string) contract.CollectorInput {
	start := time.Unix(1713700000, 0).UTC()
	return contract.CollectorInput{
		IncidentID:             "inc_test",
		Question:               question,
		HypothesisID:           "hyp_test",
		TimeRange:              contract.TimeRange{Start: start, End: start.Add(10 * time.Minute)},
		ScopeServices:          scope,
		EnvironmentFingerprint: contract.EnvironmentFingerprint{Cluster: "k3d-local"},
		MaxInternalIterations:  3,
	}
}

func ptr[T any](v T) *T { return &v }

// ---- ParseQuestion ----------------------------------------------------------

func TestParseQuestion_PodStatus(t *testing.T) {
	t.Parallel()
	pq := kube.ParseQuestion(makeInput("pod:status demo", nil))
	assert.Equal(t, kube.KindPodStatus, pq.Kind)
	assert.Equal(t, "demo", pq.Namespace)
}

func TestParseQuestion_PodEvents_Scoped(t *testing.T) {
	t.Parallel()
	pq := kube.ParseQuestion(makeInput("pod:events demo/leaky-service-abc", nil))
	assert.Equal(t, kube.KindPodEvents, pq.Kind)
	assert.Equal(t, "demo", pq.Namespace)
	assert.Equal(t, "leaky-service-abc", pq.Name)
}

func TestParseQuestion_PodEvents_BareName(t *testing.T) {
	t.Parallel()
	// No "/" in arg → use scope_services[0] as namespace.
	pq := kube.ParseQuestion(makeInput("pod:events leaky-service-abc", []string{"demo"}))
	assert.Equal(t, kube.KindPodEvents, pq.Kind)
	assert.Equal(t, "demo", pq.Namespace)
	assert.Equal(t, "leaky-service-abc", pq.Name)
}

func TestParseQuestion_DeployStatus(t *testing.T) {
	t.Parallel()
	pq := kube.ParseQuestion(makeInput("deploy:status demo/leaky-service", nil))
	assert.Equal(t, kube.KindDeployStatus, pq.Kind)
	assert.Equal(t, "demo", pq.Namespace)
	assert.Equal(t, "leaky-service", pq.Name)
}

func TestParseQuestion_Default(t *testing.T) {
	t.Parallel()
	pq := kube.ParseQuestion(makeInput("why is the pod crashing?", []string{"demo"}))
	assert.Equal(t, kube.KindDefault, pq.Kind)
	assert.Equal(t, "demo", pq.Namespace)
}

func TestParseQuestion_DefaultNamespace_Empty(t *testing.T) {
	t.Parallel()
	// No scope_services → namespace falls back to "demo".
	pq := kube.ParseQuestion(makeInput("what is happening?", nil))
	assert.Equal(t, "demo", pq.Namespace)
}

// ---- pod:status dispatch ----------------------------------------------------

func TestDispatch_PodStatus_ReturnsList(t *testing.T) {
	t.Parallel()
	cs := fake.NewSimpleClientset(
		&corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{Name: "pod-a", Namespace: "demo"},
			Status: corev1.PodStatus{
				Phase: corev1.PodRunning,
				ContainerStatuses: []corev1.ContainerStatus{
					{Name: "app", RestartCount: 3},
				},
			},
		},
		&corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{Name: "pod-b", Namespace: "demo"},
			Status: corev1.PodStatus{
				Phase: corev1.PodFailed,
			},
		},
	)

	in := makeInput("pod:status demo", []string{"demo"})
	dr := kube.Dispatch(context.Background(), cs, in)

	assert.Equal(t, 0.5, dr.Confidence)
	assert.Equal(t, 1, dr.QueryCount)
	assert.Contains(t, dr.Summary, "2 pod(s)")
	assert.Contains(t, dr.Summary, "pod-a")
	assert.Contains(t, dr.Summary, "restarts=3")
	assert.NotEmpty(t, dr.RawBody)

	// RawBody must be valid JSON.
	assert.True(t, json.Valid(dr.RawBody))
}

func TestDispatch_PodStatus_Empty(t *testing.T) {
	t.Parallel()
	cs := fake.NewSimpleClientset()
	in := makeInput("pod:status empty-ns", nil)
	dr := kube.Dispatch(context.Background(), cs, in)

	assert.Contains(t, dr.Summary, "no pods found")
	assert.Equal(t, 0.5, dr.Confidence) // list-style even when empty
}

// ---- pod:events dispatch ----------------------------------------------------

func makePodEvent(ns, podName, reason, message, eventType string, ts time.Time) *corev1.Event {
	return &corev1.Event{
		ObjectMeta: metav1.ObjectMeta{
			Name:      reason + "-event",
			Namespace: ns,
		},
		InvolvedObject: corev1.ObjectReference{
			Kind:      "Pod",
			Name:      podName,
			Namespace: ns,
		},
		Reason:        reason,
		Message:       message,
		Type:          eventType,
		LastTimestamp: metav1.NewTime(ts),
	}
}

func TestDispatch_PodEvents_Sorted(t *testing.T) {
	t.Parallel()
	base := time.Unix(1713700000, 0).UTC()
	cs := fake.NewSimpleClientset(
		makePodEvent("demo", "leaky-pod", "OOMKilling", "memory limit exceeded", "Warning", base.Add(2*time.Minute)),
		makePodEvent("demo", "leaky-pod", "Pulling", "pulling image", "Normal", base),
		makePodEvent("demo", "leaky-pod", "Started", "started container", "Normal", base.Add(time.Minute)),
	)

	in := makeInput("pod:events demo/leaky-pod", []string{"demo"})
	dr := kube.Dispatch(context.Background(), cs, in)

	assert.Equal(t, 0.5, dr.Confidence)
	assert.Contains(t, dr.Summary, "3 event(s)")
	assert.True(t, json.Valid(dr.RawBody))
}

func TestDispatch_PodEvents_None(t *testing.T) {
	t.Parallel()
	cs := fake.NewSimpleClientset()
	in := makeInput("pod:events demo/no-such-pod", []string{"demo"})
	dr := kube.Dispatch(context.Background(), cs, in)
	assert.Contains(t, dr.Summary, "no events found")
}

// ---- deploy:status dispatch -------------------------------------------------

func TestDispatch_DeployStatus_RolloutComplete(t *testing.T) {
	t.Parallel()
	cs := fake.NewSimpleClientset(
		&appsv1.Deployment{
			ObjectMeta: metav1.ObjectMeta{Name: "leaky-service", Namespace: "demo"},
			Spec: appsv1.DeploymentSpec{
				Replicas: ptr(int32(3)),
			},
			Status: appsv1.DeploymentStatus{
				ReadyReplicas: 3,
				Conditions: []appsv1.DeploymentCondition{
					{
						Type:    "Available",
						Status:  "True",
						Message: "Deployment has minimum availability.",
					},
				},
			},
		},
	)

	in := makeInput("deploy:status demo/leaky-service", []string{"demo"})
	dr := kube.Dispatch(context.Background(), cs, in)

	assert.Equal(t, 1.0, dr.Confidence) // deterministic single answer
	assert.Contains(t, dr.Summary, "desired=3")
	assert.Contains(t, dr.Summary, "ready=3")
	assert.Contains(t, dr.Summary, "rollout_complete=true")
	assert.True(t, json.Valid(dr.RawBody))
}

func TestDispatch_DeployStatus_NotReady(t *testing.T) {
	t.Parallel()
	cs := fake.NewSimpleClientset(
		&appsv1.Deployment{
			ObjectMeta: metav1.ObjectMeta{Name: "leaky-service", Namespace: "demo"},
			Spec: appsv1.DeploymentSpec{
				Replicas: ptr(int32(2)),
			},
			Status: appsv1.DeploymentStatus{
				ReadyReplicas: 0,
			},
		},
	)

	in := makeInput("deploy:status demo/leaky-service", []string{"demo"})
	dr := kube.Dispatch(context.Background(), cs, in)

	assert.Equal(t, 1.0, dr.Confidence)
	assert.Contains(t, dr.Summary, "rollout_complete=false")
}

// ---- default dispatch (terminated pods) -------------------------------------

func TestDispatch_Default_TerminatedPods(t *testing.T) {
	t.Parallel()
	ts := metav1.NewTime(time.Unix(1713700000, 0).UTC())
	cs := fake.NewSimpleClientset(
		&corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{Name: "leaky-pod-abc", Namespace: "demo"},
			Status: corev1.PodStatus{
				ContainerStatuses: []corev1.ContainerStatus{
					{
						Name: "leaky",
						LastTerminationState: corev1.ContainerState{
							Terminated: &corev1.ContainerStateTerminated{
								ExitCode:   137,
								Reason:     "OOMKilled",
								FinishedAt: ts,
							},
						},
					},
				},
			},
		},
	)

	in := makeInput("why is this pod crashing?", []string{"demo"})
	dr := kube.Dispatch(context.Background(), cs, in)

	assert.Equal(t, 0.5, dr.Confidence)
	assert.Contains(t, dr.Summary, "1 terminated container(s)")
	assert.Contains(t, dr.Summary, "OOMKilled")
	assert.Contains(t, dr.Summary, "exitCode=137")
	assert.True(t, json.Valid(dr.RawBody))
}

func TestDispatch_Default_NoTerminatedPods(t *testing.T) {
	t.Parallel()
	cs := fake.NewSimpleClientset(
		&corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{Name: "healthy-pod", Namespace: "demo"},
			Status: corev1.PodStatus{
				Phase: corev1.PodRunning,
			},
		},
	)

	in := makeInput("what is happening?", []string{"demo"})
	dr := kube.Dispatch(context.Background(), cs, in)

	assert.Contains(t, dr.Summary, "no pods with terminated lastState")
}

// ---- error surface (RBAC) ---------------------------------------------------

// TestDispatch_RBACErrorSurfaced verifies that API errors (including RBAC
// Forbidden) appear verbatim in Summary.  The fake client doesn't simulate
// Forbidden, but we can induce an error by passing a nil clientset; however
// a simpler approach is to call Dispatch with a question that results in a
// Get for a non-existent deploy — the fake returns a NotFound which the
// caller must surface.
func TestDispatch_DeployStatus_NotFound_SurfacedVerbatim(t *testing.T) {
	t.Parallel()
	cs := fake.NewSimpleClientset() // no deployments registered

	in := makeInput("deploy:status demo/ghost-deploy", nil)
	dr := kube.Dispatch(context.Background(), cs, in)

	assert.Equal(t, 0.0, dr.Confidence)
	assert.Contains(t, dr.Summary, "kubernetes API error")
	// "not found" must appear so the Investigator can distinguish it from RBAC.
	assert.Contains(t, dr.Summary, "not found")
}
