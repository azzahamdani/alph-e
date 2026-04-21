// Package prom provides a minimal typed wrapper over the Prometheus HTTP API.
// It uses only stdlib net/http and encoding/json — no third-party Prometheus
// client library is pulled in, keeping the dependency graph small per ADR-0003.
package prom

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// Sample is a single (timestamp, value) pair from a Prometheus result.
type Sample struct {
	Timestamp time.Time
	Value     float64
}

// Series is a labelled stream of samples returned by query_range.
type Series struct {
	Metric  map[string]string
	Samples []Sample
}

// InstantResult holds the result of an instant /api/v1/query call.
type InstantResult struct {
	Metric  map[string]string
	Value   Sample
	RawBody []byte // verbatim JSON response, stored as evidence
}

// RangeResult holds the result of a /api/v1/query_range call.
type RangeResult struct {
	Series  []Series
	RawBody []byte // verbatim JSON response, stored as evidence
}

// Client issues queries against a Prometheus HTTP API endpoint.
type Client struct {
	baseURL    string
	httpClient *http.Client
}

// NewClient constructs a Client targeting baseURL (e.g. "http://localhost:9090").
// A nil httpClient falls back to http.DefaultClient.
func NewClient(baseURL string, httpClient *http.Client) *Client {
	if httpClient == nil {
		httpClient = http.DefaultClient
	}
	return &Client{baseURL: baseURL, httpClient: httpClient}
}

// --- Prometheus wire types ---

type apiResponse struct {
	Status    string          `json:"status"`
	ErrorType string          `json:"errorType,omitempty"`
	Error     string          `json:"error,omitempty"`
	Data      json.RawMessage `json:"data,omitempty"`
}

type queryData struct {
	ResultType string          `json:"resultType"`
	Result     json.RawMessage `json:"result"`
}

// vectorResult is a single element from a "vector" resultType.
type vectorResult struct {
	Metric map[string]string  `json:"metric"`
	Value  [2]json.RawMessage `json:"value"` // [timestamp, "value"]
}

// matrixResult is a single element from a "matrix" resultType.
type matrixResult struct {
	Metric map[string]string    `json:"metric"`
	Values [][2]json.RawMessage `json:"values"` // [[timestamp, "value"], ...]
}

// --- helpers ---

func parseSample(raw [2]json.RawMessage) (Sample, error) {
	var ts float64
	if err := json.Unmarshal(raw[0], &ts); err != nil {
		return Sample{}, fmt.Errorf("parse timestamp: %w", err)
	}
	var valStr string
	if err := json.Unmarshal(raw[1], &valStr); err != nil {
		return Sample{}, fmt.Errorf("parse value string: %w", err)
	}
	v, err := strconv.ParseFloat(valStr, 64)
	if err != nil {
		return Sample{}, fmt.Errorf("parse value float %q: %w", valStr, err)
	}
	return Sample{
		Timestamp: time.Unix(int64(ts), 0).UTC(),
		Value:     v,
	}, nil
}

func (c *Client) doGET(ctx context.Context, path string, params url.Values) ([]byte, error) {
	u, err := url.Parse(c.baseURL + path)
	if err != nil {
		return nil, fmt.Errorf("build URL: %w", err)
	}
	u.RawQuery = params.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), http.NoBody)
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("http GET %s: %w", u.String(), err)
	}
	defer resp.Body.Close() //nolint:errcheck // response body close error is not actionable

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read response body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		// Surface Prometheus error payload verbatim (guardrail requirement).
		var apiErr apiResponse
		if jsonErr := json.Unmarshal(body, &apiErr); jsonErr == nil && apiErr.Error != "" {
			return nil, fmt.Errorf("prometheus %s: %s", apiErr.ErrorType, apiErr.Error)
		}
		return nil, fmt.Errorf("prometheus HTTP %d: %s", resp.StatusCode, string(body))
	}

	var apiResp apiResponse
	if err := json.Unmarshal(body, &apiResp); err != nil {
		return nil, fmt.Errorf("decode prometheus envelope: %w", err)
	}
	if apiResp.Status != "success" {
		return nil, fmt.Errorf("prometheus %s: %s", apiResp.ErrorType, apiResp.Error)
	}

	return body, nil
}

// QueryInstant executes a PromQL instant query at the given time.
// The time parameter must be non-zero (guardrail: never query without a time).
func (c *Client) QueryInstant(ctx context.Context, expr string, at time.Time) (InstantResult, error) {
	if at.IsZero() {
		return InstantResult{}, fmt.Errorf("QueryInstant: at must not be zero")
	}

	params := url.Values{}
	params.Set("query", expr)
	params.Set("time", strconv.FormatFloat(float64(at.Unix()), 'f', 0, 64))

	body, err := c.doGET(ctx, "/api/v1/query", params)
	if err != nil {
		return InstantResult{RawBody: body}, err
	}

	var envelope apiResponse
	if err := json.Unmarshal(body, &envelope); err != nil {
		return InstantResult{RawBody: body}, fmt.Errorf("decode envelope: %w", err)
	}

	var data queryData
	if err := json.Unmarshal(envelope.Data, &data); err != nil {
		return InstantResult{RawBody: body}, fmt.Errorf("decode data: %w", err)
	}

	var rows []vectorResult
	if err := json.Unmarshal(data.Result, &rows); err != nil {
		return InstantResult{RawBody: body}, fmt.Errorf("decode vector: %w", err)
	}

	if len(rows) == 0 {
		return InstantResult{RawBody: body}, nil
	}

	sample, err := parseSample(rows[0].Value)
	if err != nil {
		return InstantResult{RawBody: body}, err
	}

	return InstantResult{
		Metric:  rows[0].Metric,
		Value:   sample,
		RawBody: body,
	}, nil
}

// QueryRange executes a PromQL range query over [start, end) with the given step.
// Both start and end must be non-zero (guardrail: never query without a time range).
func (c *Client) QueryRange(ctx context.Context, expr string, start, end time.Time, step time.Duration) (RangeResult, error) {
	if start.IsZero() || end.IsZero() {
		return RangeResult{}, fmt.Errorf("QueryRange: start and end must not be zero")
	}
	if !end.After(start) {
		return RangeResult{}, fmt.Errorf("QueryRange: end must be after start")
	}
	if step <= 0 {
		step = 60 * time.Second // safe default
	}

	params := url.Values{}
	params.Set("query", expr)
	params.Set("start", strconv.FormatFloat(float64(start.Unix()), 'f', 0, 64))
	params.Set("end", strconv.FormatFloat(float64(end.Unix()), 'f', 0, 64))
	params.Set("step", strconv.FormatFloat(step.Seconds(), 'f', 0, 64))

	body, err := c.doGET(ctx, "/api/v1/query_range", params)
	if err != nil {
		return RangeResult{RawBody: body}, err
	}

	var envelope apiResponse
	if err := json.Unmarshal(body, &envelope); err != nil {
		return RangeResult{RawBody: body}, fmt.Errorf("decode envelope: %w", err)
	}

	var data queryData
	if err := json.Unmarshal(envelope.Data, &data); err != nil {
		return RangeResult{RawBody: body}, fmt.Errorf("decode data: %w", err)
	}

	var rows []matrixResult
	if err := json.Unmarshal(data.Result, &rows); err != nil {
		return RangeResult{RawBody: body}, fmt.Errorf("decode matrix: %w", err)
	}

	series := make([]Series, 0, len(rows))
	for _, row := range rows {
		s := Series{Metric: row.Metric}
		s.Samples = make([]Sample, 0, len(row.Values))
		for _, v := range row.Values {
			sample, err := parseSample(v)
			if err != nil {
				return RangeResult{RawBody: body}, err
			}
			s.Samples = append(s.Samples, sample)
		}
		series = append(series, s)
	}

	return RangeResult{Series: series, RawBody: body}, nil
}
