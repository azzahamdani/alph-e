package loki

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// LogLine is the NDJSON record written to the evidence store for each log hit.
type LogLine struct {
	TimestampNs string            `json:"ts_ns"`
	Line        string            `json:"line"`
	Labels      map[string]string `json:"labels"`
}

// DispatchRequest bundles all parameters for a LogQL dispatch call.
type DispatchRequest struct {
	// Question is the raw question from CollectorInput; used to derive LogQL.
	Question string
	// Namespace is the primary k8s namespace to scope the query to.
	Namespace string
	// ScopeServices narrows the stream selector when non-empty.
	ScopeServices []string
	// Start and End define the time window; both are required.
	Start time.Time
	End   time.Time
	// MaxLines caps total log lines collected; must be > 0.
	MaxLines int
}

// DispatchResult holds the output of a successful dispatch.
type DispatchResult struct {
	// NDJSON is the raw evidence payload — one JSON object per line.
	NDJSON []byte
	// MatchCount is the number of log lines collected.
	MatchCount int
	// LogQL is the expression that was actually executed.
	LogQL string
}

// Dispatch derives a LogQL expression from req.Question, executes it against
// Loki, and returns the evidence payload together with match metadata.
//
// LogQL derivation rules (MVP1):
//
//   - "log:contains <phrase>" → {namespace="<ns>"} |= "<phrase>"
//   - "log:rate <pattern>"    → sum(rate({…} |~ "<pattern>" [5m]))
//   - default                 → {namespace="<ns>"} |~ "(?i)error|fatal|panic|oom"
func Dispatch(ctx context.Context, client *Client, req *DispatchRequest) (DispatchResult, error) {
	if req.MaxLines <= 0 {
		return DispatchResult{}, fmt.Errorf("loki dispatch: MaxLines must be > 0")
	}
	if req.Start.IsZero() || req.End.IsZero() {
		return DispatchResult{}, fmt.Errorf("loki dispatch: Start and End are required")
	}
	if !req.End.After(req.Start) {
		return DispatchResult{}, fmt.Errorf("loki dispatch: End must be after Start")
	}

	logQL := buildLogQL(req.Question, req.Namespace, req.ScopeServices)

	streams, err := client.QueryRange(ctx, logQL, req.Start, req.End, req.MaxLines)
	if err != nil {
		return DispatchResult{}, fmt.Errorf("loki dispatch: %w", err)
	}

	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	count := 0

	for _, stream := range streams {
		for _, val := range stream.Values {
			if count >= req.MaxLines {
				break
			}
			rec := LogLine{
				TimestampNs: val[0],
				Line:        val[1],
				Labels:      stream.Labels,
			}
			if err := enc.Encode(rec); err != nil {
				return DispatchResult{}, fmt.Errorf("loki dispatch: encode log line: %w", err)
			}
			count++
		}
		if count >= req.MaxLines {
			break
		}
	}

	return DispatchResult{
		NDJSON:     buf.Bytes(),
		MatchCount: count,
		LogQL:      logQL,
	}, nil
}

// buildLogQL derives the LogQL expression from the question string following
// the MVP1 pattern set.
func buildLogQL(question, namespace string, scopeServices []string) string {
	selector := buildSelector(namespace, scopeServices)

	switch {
	case strings.HasPrefix(question, "log:contains "):
		phrase := strings.TrimPrefix(question, "log:contains ")
		phrase = strings.TrimSpace(phrase)
		return fmt.Sprintf(`%s |= %q`, selector, phrase)

	case strings.HasPrefix(question, "log:rate "):
		pattern := strings.TrimPrefix(question, "log:rate ")
		pattern = strings.TrimSpace(pattern)
		return fmt.Sprintf(`sum(rate(%s |~ %q [5m]))`, selector, pattern)

	default:
		return fmt.Sprintf(`%s |~ "(?i)error|fatal|panic|oom"`, selector)
	}
}

// buildSelector constructs a Loki stream selector for the given namespace and
// optional service list. When services are present, a job or app label is added.
func buildSelector(namespace string, scopeServices []string) string {
	if len(scopeServices) == 0 {
		return fmt.Sprintf(`{namespace=%q}`, namespace)
	}
	// Multiple services: use a regex alternation on the "app" label.
	pattern := strings.Join(scopeServices, "|")
	return fmt.Sprintf(`{namespace=%q, app=~%q}`, namespace, pattern)
}
