// Package loki provides a minimal HTTP client for the Loki query_range API.
// It uses only the Go standard library — no third-party Loki SDK.
package loki

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

// Stream is one label-set + its matching log lines from a Loki result.
type Stream struct {
	// Labels holds the stream's label set, e.g. {"namespace":"demo","pod":"..."}
	Labels map[string]string `json:"stream"`
	// Values is a list of [timestamp_ns, log_line] pairs.
	Values [][2]string `json:"values"`
}

// queryRangeResponse is the envelope Loki returns for /loki/api/v1/query_range.
type queryRangeResponse struct {
	Data struct {
		ResultType string   `json:"resultType"`
		Result     []Stream `json:"result"`
	} `json:"data"`
}

// Client issues LogQL queries against a Loki HTTP endpoint.
type Client struct {
	baseURL    string
	httpClient *http.Client
}

// NewClient constructs a Client. baseURL must not have a trailing slash,
// e.g. "http://loki.monitoring.svc.cluster.local:3100".
func NewClient(baseURL string, timeout time.Duration) *Client {
	if timeout == 0 {
		timeout = 30 * time.Second
	}
	return &Client{
		baseURL:    baseURL,
		httpClient: &http.Client{Timeout: timeout},
	}
}

// QueryRange calls /loki/api/v1/query_range and returns all streams within the
// response. start and end are inclusive epoch nanoseconds (Loki accepts both
// nanosecond and RFC3339 strings; we use nanoseconds for precision). limit caps
// the number of log lines Loki returns per request.
func (c *Client) QueryRange(
	ctx context.Context,
	logQL string,
	start, end time.Time,
	limit int,
) ([]Stream, error) {
	u, err := url.Parse(c.baseURL + "/loki/api/v1/query_range")
	if err != nil {
		return nil, fmt.Errorf("loki: parse base URL: %w", err)
	}

	q := u.Query()
	q.Set("query", logQL)
	// Loki accepts Unix nanoseconds as strings.
	q.Set("start", strconv.FormatInt(start.UnixNano(), 10))
	q.Set("end", strconv.FormatInt(end.UnixNano(), 10))
	if limit > 0 {
		q.Set("limit", strconv.Itoa(limit))
	}
	// direction=forward gives chronological order.
	q.Set("direction", "forward")
	u.RawQuery = q.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), http.NoBody)
	if err != nil {
		return nil, fmt.Errorf("loki: build request: %w", err)
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("loki: query_range: %w", err)
	}
	defer resp.Body.Close() //nolint:errcheck // best-effort drain; error is non-actionable here

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("loki: read body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("loki: unexpected status %d: %s", resp.StatusCode, body)
	}

	var qr queryRangeResponse
	if err := json.Unmarshal(body, &qr); err != nil {
		return nil, fmt.Errorf("loki: decode response: %w", err)
	}

	return qr.Data.Result, nil
}
