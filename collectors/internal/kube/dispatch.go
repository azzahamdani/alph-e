package kube

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"

	"github.com/Matt-LiFi/alph-e/collectors/internal/contract"
)

// QueryKind classifies the parsed question.
type QueryKind int

const (
	// KindPodStatus maps to "pod:status <namespace>".
	KindPodStatus QueryKind = iota
	// KindPodEvents maps to "pod:events <namespace>/<name>".
	KindPodEvents
	// KindDeployStatus maps to "deploy:status <namespace>/<name>".
	KindDeployStatus
	// KindDefault lists terminated pods in scope_services[0].
	KindDefault
)

// ParsedQuery holds the structured interpretation of a CollectorInput question.
type ParsedQuery struct {
	Kind      QueryKind
	Namespace string
	Name      string // pod or deploy name (empty for namespace-wide queries)
}

// ParseQuestion converts the free-text question and scope_services list into a
// ParsedQuery.  Supported patterns:
//
//	pod:status <namespace>
//	pod:events <namespace>/<name>
//	deploy:status <namespace>/<name>
//	<anything else> → KindDefault using scope_services[0] as namespace
func ParseQuestion(in contract.CollectorInput) ParsedQuery {
	ns := defaultNamespace(in.ScopeServices)
	q := strings.TrimSpace(in.Question)

	switch {
	case strings.HasPrefix(q, "pod:status "):
		arg := strings.TrimPrefix(q, "pod:status ")
		return ParsedQuery{Kind: KindPodStatus, Namespace: strings.TrimSpace(arg)}

	case strings.HasPrefix(q, "pod:events "):
		arg := strings.TrimPrefix(q, "pod:events ")
		return parseScopedName(KindPodEvents, arg, ns)

	case strings.HasPrefix(q, "deploy:status "):
		arg := strings.TrimPrefix(q, "deploy:status ")
		return parseScopedName(KindDeployStatus, arg, ns)

	default:
		return ParsedQuery{Kind: KindDefault, Namespace: ns}
	}
}

// parseScopedName parses "<namespace>/<name>" or falls back to defaultNS.
func parseScopedName(kind QueryKind, arg, defaultNS string) ParsedQuery {
	arg = strings.TrimSpace(arg)
	parts := strings.SplitN(arg, "/", 2)
	if len(parts) == 2 {
		return ParsedQuery{Kind: kind, Namespace: parts[0], Name: parts[1]}
	}
	// Bare name — use default namespace.
	return ParsedQuery{Kind: kind, Namespace: defaultNS, Name: arg}
}

func defaultNamespace(scopeServices []string) string {
	if len(scopeServices) > 0 {
		// strip any trailing "/service" suffix.
		parts := strings.SplitN(scopeServices[0], "/", 2)
		if parts[0] != "" {
			return parts[0]
		}
	}
	return "demo"
}

// DispatchResult bundles the raw API response with a ready-made summary and
// confidence score.
type DispatchResult struct {
	RawBody    []byte
	Summary    string
	Confidence float64
	QueryCount int
}

// Dispatch routes in to the correct k8s API call and returns a DispatchResult.
// It never issues write verbs.  RBAC errors are surfaced verbatim in Summary so
// the Investigator can suggest permission fixes.
func Dispatch(
	ctx context.Context,
	cs kubernetes.Interface,
	in contract.CollectorInput,
) DispatchResult {
	pq := ParseQuestion(in)
	switch pq.Kind {
	case KindPodStatus:
		return dispatchPodStatus(ctx, cs, pq)
	case KindPodEvents:
		return dispatchPodEvents(ctx, cs, pq)
	case KindDeployStatus:
		return dispatchDeployStatus(ctx, cs, pq)
	default:
		return dispatchDefault(ctx, cs, pq)
	}
}

// --- pod:status <namespace> ---

type podStatusRow struct {
	Name         string `json:"name"`
	Phase        string `json:"phase"`
	RestartCount int32  `json:"restart_count"`
}

func dispatchPodStatus(ctx context.Context, cs kubernetes.Interface, pq ParsedQuery) DispatchResult {
	list, err := cs.CoreV1().Pods(pq.Namespace).List(ctx, metav1.ListOptions{})
	if err != nil {
		return errResult(nil, err, 1)
	}

	rows := make([]podStatusRow, 0, len(list.Items))
	for i := range list.Items {
		p := &list.Items[i]
		var restarts int32
		for j := range p.Status.ContainerStatuses {
			restarts += p.Status.ContainerStatuses[j].RestartCount
		}
		rows = append(rows, podStatusRow{
			Name:         p.Name,
			Phase:        string(p.Status.Phase),
			RestartCount: restarts,
		})
	}

	raw, _ := json.Marshal(list) //nolint:errcheck
	summary := buildPodStatusSummary(pq.Namespace, rows)
	return DispatchResult{
		RawBody:    raw,
		Summary:    summary,
		Confidence: 0.5, // list-style
		QueryCount: 1,
	}
}

func buildPodStatusSummary(ns string, rows []podStatusRow) string {
	if len(rows) == 0 {
		return fmt.Sprintf("no pods found in namespace %q", ns)
	}
	parts := make([]string, 0, len(rows)+1)
	parts = append(parts, fmt.Sprintf("%d pod(s) in namespace %q:", len(rows), ns))
	for _, r := range rows {
		parts = append(parts, fmt.Sprintf("[%s phase=%s restarts=%d]", r.Name, r.Phase, r.RestartCount))
	}
	return strings.Join(parts, " ")
}

// --- pod:events <namespace>/<name> ---

type eventRow struct {
	LastTimestamp string `json:"last_timestamp"`
	Reason        string `json:"reason"`
	Message       string `json:"message"`
	Type          string `json:"type"`
}

func dispatchPodEvents(ctx context.Context, cs kubernetes.Interface, pq ParsedQuery) DispatchResult {
	fieldSelector := fmt.Sprintf("involvedObject.name=%s,involvedObject.namespace=%s,involvedObject.kind=Pod",
		pq.Name, pq.Namespace)
	list, err := cs.CoreV1().Events(pq.Namespace).List(ctx, metav1.ListOptions{
		FieldSelector: fieldSelector,
	})
	if err != nil {
		return errResult(nil, err, 1)
	}

	// Sort by lastTimestamp ascending so the most recent event is last.
	events := list.Items
	sort.Slice(events, func(i, j int) bool {
		ti := events[i].LastTimestamp.Time
		tj := events[j].LastTimestamp.Time
		return ti.Before(tj)
	})

	rows := make([]eventRow, 0, len(events))
	for i := range events {
		ev := &events[i]
		rows = append(rows, eventRow{
			LastTimestamp: ev.LastTimestamp.UTC().Format("2006-01-02T15:04:05Z"),
			Reason:        ev.Reason,
			Message:       ev.Message,
			Type:          ev.Type,
		})
	}

	raw, _ := json.Marshal(list) //nolint:errcheck
	summary := buildEventsSummary(pq.Namespace, pq.Name, rows)
	return DispatchResult{
		RawBody:    raw,
		Summary:    summary,
		Confidence: 0.5, // list-style
		QueryCount: 1,
	}
}

func buildEventsSummary(ns, name string, rows []eventRow) string {
	if len(rows) == 0 {
		return fmt.Sprintf("no events found for pod %s/%s", ns, name)
	}
	parts := make([]string, 0, len(rows)+1)
	parts = append(parts, fmt.Sprintf("%d event(s) for pod %s/%s (chronological):", len(rows), ns, name))
	for _, r := range rows {
		parts = append(parts, fmt.Sprintf("[%s %s: %s]", r.LastTimestamp, r.Reason, r.Message))
	}
	return strings.Join(parts, " ")
}

// --- deploy:status <namespace>/<name> ---

type deployStatusRow struct {
	Name            string `json:"name"`
	Desired         int32  `json:"desired"`
	Ready           int32  `json:"ready"`
	RolloutComplete bool   `json:"rollout_complete"`
	Condition       string `json:"condition"`
}

func dispatchDeployStatus(ctx context.Context, cs kubernetes.Interface, pq ParsedQuery) DispatchResult {
	deploy, err := cs.AppsV1().Deployments(pq.Namespace).Get(ctx, pq.Name, metav1.GetOptions{})
	if err != nil {
		return errResult(nil, err, 1)
	}

	var desired, ready int32
	if deploy.Spec.Replicas != nil {
		desired = *deploy.Spec.Replicas
	}
	ready = deploy.Status.ReadyReplicas

	condMsg := ""
	rolloutComplete := ready == desired
	for i := range deploy.Status.Conditions {
		c := &deploy.Status.Conditions[i]
		if c.Type == appsv1.DeploymentAvailable {
			condMsg = fmt.Sprintf("Available=%s (%s)", c.Status, c.Message)
		}
	}

	row := deployStatusRow{
		Name:            deploy.Name,
		Desired:         desired,
		Ready:           ready,
		RolloutComplete: rolloutComplete,
		Condition:       condMsg,
	}

	raw, _ := json.Marshal(deploy) //nolint:errcheck
	summary := fmt.Sprintf("deploy %s/%s: desired=%d ready=%d rollout_complete=%v %s",
		pq.Namespace, pq.Name, row.Desired, row.Ready, row.RolloutComplete, row.Condition)

	return DispatchResult{
		RawBody:    raw,
		Summary:    summary,
		Confidence: 1.0, // single deterministic answer
		QueryCount: 1,
	}
}

// --- default: list terminated pods in scope_services[0] namespace ---

type terminatedPodRow struct {
	Name          string `json:"name"`
	ContainerName string `json:"container_name"`
	ExitCode      int32  `json:"exit_code"`
	Reason        string `json:"reason"`
	FinishedAt    string `json:"finished_at"`
}

func dispatchDefault(ctx context.Context, cs kubernetes.Interface, pq ParsedQuery) DispatchResult {
	list, err := cs.CoreV1().Pods(pq.Namespace).List(ctx, metav1.ListOptions{})
	if err != nil {
		return errResult(nil, err, 1)
	}

	rows := terminatedPods(list.Items)
	raw, _ := json.Marshal(list) //nolint:errcheck
	summary := buildTerminatedSummary(pq.Namespace, rows)
	return DispatchResult{
		RawBody:    raw,
		Summary:    summary,
		Confidence: 0.5, // list-style
		QueryCount: 1,
	}
}

// terminatedPods extracts pods whose lastState is Terminated.
func terminatedPods(pods []corev1.Pod) []terminatedPodRow {
	var rows []terminatedPodRow
	for i := range pods {
		p := &pods[i]
		for j := range p.Status.ContainerStatuses {
			cs := &p.Status.ContainerStatuses[j]
			if cs.LastTerminationState.Terminated == nil {
				continue
			}
			t := cs.LastTerminationState.Terminated
			rows = append(rows, terminatedPodRow{
				Name:          p.Name,
				ContainerName: cs.Name,
				ExitCode:      t.ExitCode,
				Reason:        t.Reason,
				FinishedAt:    t.FinishedAt.UTC().Format("2006-01-02T15:04:05Z"),
			})
		}
	}
	return rows
}

func buildTerminatedSummary(ns string, rows []terminatedPodRow) string {
	if len(rows) == 0 {
		return fmt.Sprintf("no pods with terminated lastState found in namespace %q", ns)
	}
	parts := make([]string, 0, len(rows)+1)
	parts = append(parts, fmt.Sprintf("%d terminated container(s) in namespace %q:", len(rows), ns))
	for _, r := range rows {
		parts = append(parts, fmt.Sprintf("[pod=%s container=%s exitCode=%d reason=%s finishedAt=%s]",
			r.Name, r.ContainerName, r.ExitCode, r.Reason, r.FinishedAt))
	}
	return strings.Join(parts, " ")
}

// errResult constructs a zero-confidence DispatchResult from an API error.
// RBAC errors are surfaced verbatim per the guardrail requirement.
func errResult(raw []byte, err error, queryCount int) DispatchResult {
	return DispatchResult{
		RawBody:    raw,
		Summary:    fmt.Sprintf("kubernetes API error: %s", err.Error()),
		Confidence: 0,
		QueryCount: queryCount,
	}
}
