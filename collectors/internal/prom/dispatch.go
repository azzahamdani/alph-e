package prom

import (
	"context"
	"fmt"
	"math"
	"strings"
	"time"

	"github.com/Matt-LiFi/alph-e/collectors/internal/contract"
)

// QueryKind classifies the question pattern so dispatch can choose the right
// Prometheus API endpoint.
type QueryKind int

const (
	// KindRate maps to query_range (metric:rate <metric> <op> <threshold>).
	KindRate QueryKind = iota
	// KindValue maps to an instant query (metric:value <metric>).
	KindValue
	// KindDefault is the fall-through: query OOM reason counts by namespace.
	KindDefault
)

// ParsedQuery is the result of interpreting a CollectorInput question.
type ParsedQuery struct {
	Kind       QueryKind
	Expr       string  // PromQL expression
	Comparator string  // ">" | ">=" | "<" | "<=" | "==" | "!=" (KindRate only)
	Threshold  float64 // numeric threshold (KindRate only)
}

// ParseQuestion interprets the question field and the scope_services list into
// a ParsedQuery.  The supported patterns are:
//
//	metric:rate <metric> <comparator> <threshold>
//	metric:value <metric>
//	<anything else> → KindDefault
//
// Namespace filter is derived from ScopeServices when present; defaults to
// "demo".
func ParseQuestion(in *contract.CollectorInput) ParsedQuery {
	ns := defaultNamespace(in.ScopeServices)
	q := strings.TrimSpace(in.Question)

	switch {
	case strings.HasPrefix(q, "metric:rate "):
		return parseRateQuestion(q, ns)
	case strings.HasPrefix(q, "metric:value "):
		return parseValueQuestion(q, ns)
	default:
		return ParsedQuery{
			Kind: KindDefault,
			Expr: fmt.Sprintf(
				`kube_pod_container_status_last_terminated_reason{namespace=%q}`,
				ns,
			),
		}
	}
}

func parseRateQuestion(q, ns string) ParsedQuery {
	// Format: metric:rate <metric> <comparator> <threshold>
	// e.g.   metric:rate container_memory_working_set_bytes > 100000000
	parts := strings.Fields(q)
	// parts[0] = "metric:rate", parts[1] = metric, parts[2] = comparator, parts[3] = threshold
	if len(parts) < 4 {
		// Malformed — fall back to default.
		return ParsedQuery{
			Kind: KindDefault,
			Expr: fmt.Sprintf(`kube_pod_container_status_last_terminated_reason{namespace=%q}`, ns),
		}
	}

	metric := parts[1]
	comparator := parts[2]
	var threshold float64
	_, err := fmt.Sscanf(parts[3], "%g", &threshold)
	if err != nil {
		return ParsedQuery{
			Kind: KindDefault,
			Expr: fmt.Sprintf(`kube_pod_container_status_last_terminated_reason{namespace=%q}`, ns),
		}
	}

	expr := fmt.Sprintf(`%s{namespace=%q}`, metric, ns)
	return ParsedQuery{
		Kind:       KindRate,
		Expr:       expr,
		Comparator: comparator,
		Threshold:  threshold,
	}
}

func parseValueQuestion(q, ns string) ParsedQuery {
	// Format: metric:value <metric>
	parts := strings.Fields(q)
	if len(parts) < 2 {
		return ParsedQuery{
			Kind: KindDefault,
			Expr: fmt.Sprintf(`kube_pod_container_status_last_terminated_reason{namespace=%q}`, ns),
		}
	}
	metric := parts[1]
	expr := fmt.Sprintf(`%s{namespace=%q}`, metric, ns)
	return ParsedQuery{
		Kind: KindValue,
		Expr: expr,
	}
}

func defaultNamespace(scopeServices []string) string {
	// Use the first scope service as a namespace hint if it looks like a
	// Kubernetes namespace (contains no slashes).  Otherwise default to "demo".
	if len(scopeServices) > 0 && !strings.Contains(scopeServices[0], "/") {
		// Common convention: "namespace/service" or just "namespace".
		parts := strings.SplitN(scopeServices[0], "/", 2)
		if parts[0] != "" {
			return parts[0]
		}
	}
	return "demo"
}

// DispatchResult bundles the raw evidence payload with a summarised finding.
type DispatchResult struct {
	RawBody    []byte
	Summary    string
	Confidence float64
	QueryCount int
}

// Dispatch runs up to maxIterations queries derived from in against client and
// returns a DispatchResult ready to be wrapped into a Finding + EvidenceRef.
//
// The caller is responsible for dividing the parent context deadline across
// iterations — Dispatch receives a per-call context budget.
func Dispatch(
	ctx context.Context,
	client *Client,
	in *contract.CollectorInput,
	maxIterations int,
) DispatchResult {
	pq := ParseQuestion(in)
	iterations := min(maxIterations, 1) // MVP1: one query per call

	_ = iterations // kept for future multi-query expansion

	switch pq.Kind {
	case KindRate:
		return dispatchRate(ctx, client, pq, in.TimeRange)
	case KindValue:
		return dispatchValue(ctx, client, pq, in.TimeRange)
	default:
		return dispatchDefault(ctx, client, pq, in.TimeRange)
	}
}

// step chooses a sensible step duration for a range query.
func step(tr contract.TimeRange) time.Duration {
	d := tr.End.Sub(tr.Start)
	s := d / 60 // ~60 data points across the window
	if s < 15*time.Second {
		s = 15 * time.Second
	}
	return s
}

func dispatchRate(ctx context.Context, client *Client, pq ParsedQuery, tr contract.TimeRange) DispatchResult {
	result, err := client.QueryRange(ctx, pq.Expr, tr.Start, tr.End, step(tr))
	if err != nil {
		return DispatchResult{
			RawBody:    result.RawBody,
			Summary:    fmt.Sprintf("prometheus query error: %s", err.Error()),
			Confidence: 0,
			QueryCount: 1,
		}
	}

	total := 0
	above := 0
	var latestVal float64
	var latestTS time.Time

	for _, s := range result.Series {
		for _, sample := range s.Samples {
			total++
			if compareThreshold(sample.Value, pq.Comparator, pq.Threshold) {
				above++
			}
			if sample.Timestamp.After(latestTS) {
				latestTS = sample.Timestamp
				latestVal = sample.Value
			}
		}
	}

	confidence := 0.0
	if total > 0 {
		confidence = math.Min(1.0, float64(above)/float64(total))
	}

	summary := buildRateSummary(pq, total, above, latestVal, latestTS)

	return DispatchResult{
		RawBody:    result.RawBody,
		Summary:    summary,
		Confidence: confidence,
		QueryCount: 1,
	}
}

func dispatchValue(ctx context.Context, client *Client, pq ParsedQuery, tr contract.TimeRange) DispatchResult {
	result, err := client.QueryInstant(ctx, pq.Expr, tr.End)
	if err != nil {
		return DispatchResult{
			RawBody:    result.RawBody,
			Summary:    fmt.Sprintf("prometheus query error: %s", err.Error()),
			Confidence: 0,
			QueryCount: 1,
		}
	}

	if result.Metric == nil {
		return DispatchResult{
			RawBody:    result.RawBody,
			Summary:    fmt.Sprintf("no data for %s at %s", pq.Expr, tr.End.Format(time.RFC3339)),
			Confidence: 0,
			QueryCount: 1,
		}
	}

	confidence := math.Min(1.0, result.Value.Value/1e9) // normalise: non-zero data = some signal
	if result.Value.Value > 0 {
		confidence = 1.0
	}

	summary := fmt.Sprintf(
		"%s = %.4g at %s (metric labels: %v)",
		pq.Expr,
		result.Value.Value,
		result.Value.Timestamp.Format(time.RFC3339),
		result.Metric,
	)

	return DispatchResult{
		RawBody:    result.RawBody,
		Summary:    summary,
		Confidence: confidence,
		QueryCount: 1,
	}
}

func dispatchDefault(ctx context.Context, client *Client, pq ParsedQuery, tr contract.TimeRange) DispatchResult {
	// Default: instant query at tr.End for the OOM-killed reason gauge.
	result, err := client.QueryInstant(ctx, pq.Expr, tr.End)
	if err != nil {
		return DispatchResult{
			RawBody:    result.RawBody,
			Summary:    fmt.Sprintf("prometheus query error: %s", err.Error()),
			Confidence: 0,
			QueryCount: 1,
		}
	}

	if result.Metric == nil {
		return DispatchResult{
			RawBody:    result.RawBody,
			Summary:    fmt.Sprintf("no OOM termination data for query %s at %s", pq.Expr, tr.End.Format(time.RFC3339)),
			Confidence: 0,
			QueryCount: 1,
		}
	}

	confidence := 0.0
	if result.Value.Value > 0 {
		confidence = math.Min(1.0, result.Value.Value/10.0) // each OOM event adds 0.1 confidence, cap at 1.0
	}

	summary := fmt.Sprintf(
		"OOM terminations: %.0f (reason=%s, namespace=%s, pod=%s) at %s",
		result.Value.Value,
		result.Metric["reason"],
		result.Metric["namespace"],
		result.Metric["pod"],
		result.Value.Timestamp.Format(time.RFC3339),
	)

	return DispatchResult{
		RawBody:    result.RawBody,
		Summary:    summary,
		Confidence: confidence,
		QueryCount: 1,
	}
}

func buildRateSummary(pq ParsedQuery, total, above int, latestVal float64, latestTS time.Time) string {
	if total == 0 {
		return fmt.Sprintf("no samples returned for %s", pq.Expr)
	}
	tsStr := "unknown"
	if !latestTS.IsZero() {
		tsStr = latestTS.Format(time.RFC3339)
	}
	return fmt.Sprintf(
		"%s: latest=%.4g at %s; %d/%d samples %s %.4g",
		pq.Expr,
		latestVal,
		tsStr,
		above,
		total,
		pq.Comparator,
		pq.Threshold,
	)
}

func compareThreshold(val float64, op string, threshold float64) bool {
	switch op {
	case ">":
		return val > threshold
	case ">=":
		return val >= threshold
	case "<":
		return val < threshold
	case "<=":
		return val <= threshold
	case "==":
		return val == threshold
	case "!=":
		return val != threshold
	default:
		return false
	}
}
