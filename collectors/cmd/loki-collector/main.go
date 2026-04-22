// Binary loki-collector answers hypothesis-shaped questions against Loki.
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/Matt-LiFi/alph-e/collectors/internal/contract"
	"github.com/Matt-LiFi/alph-e/collectors/internal/evidence"
	"github.com/Matt-LiFi/alph-e/collectors/internal/loki"
	"github.com/Matt-LiFi/alph-e/collectors/internal/server"
)

const (
	defaultLokiURL       = "http://loki.monitoring.svc.cluster.local:3100"
	maxLinesPerIteration = 200
	maxLinesAbsolute     = 1000
)

type lokiCollector struct {
	writer     evidence.Writer
	lokiClient *loki.Client
	logger     *slog.Logger
}

func (c *lokiCollector) Name() string { return "loki" }

//nolint:gocritic // hugeParam: CollectorInput is fixed by the server.Collector interface contract
func (c *lokiCollector) Collect(ctx context.Context, in contract.CollectorInput) (contract.CollectorOutput, error) {
	maxLines := in.EffectiveIterations() * maxLinesPerIteration
	if maxLines > maxLinesAbsolute {
		maxLines = maxLinesAbsolute
	}

	ns, services := namespaceAndServices(in.ScopeServices)

	dr, dispatchErr := loki.Dispatch(ctx, c.lokiClient, &loki.DispatchRequest{
		Question:      in.Question,
		Namespace:     ns,
		ScopeServices: services,
		Start:         in.TimeRange.Start,
		End:           in.TimeRange.End,
		MaxLines:      maxLines,
	})

	payload := dr.NDJSON
	if len(payload) == 0 {
		if dispatchErr != nil {
			payload = []byte(fmt.Sprintf(`{"error":%q,"question":%q}`, dispatchErr.Error(), in.Question))
		} else {
			payload = []byte(`{}`)
		}
	}

	ref, err := c.writer.Put(ctx, evidence.PutRequest{
		IncidentID:  in.IncidentID,
		ContentType: "application/x-ndjson",
		Payload:     payload,
	})
	if err != nil {
		return contract.CollectorOutput{}, fmt.Errorf("put evidence: %w", err)
	}

	var summary string
	var confidence float64
	if dispatchErr != nil {
		summary = fmt.Sprintf("loki query error: %s", dispatchErr.Error())
	} else {
		summary = summarise(dr)
		if dr.MatchCount > 0 {
			confidence = 0.7
		}
	}

	return contract.CollectorOutput{
		Finding: contract.Finding{
			ID:            "f_" + ref.EvidenceID,
			CollectorName: c.Name(),
			Question:      in.Question,
			Summary:       summary,
			EvidenceID:    ref.EvidenceID,
			Confidence:    confidence,
			CreatedAt:     time.Now().UTC(),
		},
		Evidence:      ref,
		ToolCallsUsed: 1,
	}, nil
}

// namespaceAndServices splits ScopeServices into a Kubernetes namespace and
// the app/service names to use as stream label selectors.
//
//   - []                    → ("demo", nil)
//   - ["demo"]              → ("demo", nil)
//   - ["demo", "leaky-svc"] → ("demo", ["leaky-svc"])
//   - ["demo/leaky-svc"]    → ("demo", ["leaky-svc"])
func namespaceAndServices(scopeServices []string) (namespace string, services []string) {
	if len(scopeServices) == 0 {
		return "demo", nil
	}
	first := scopeServices[0]
	if idx := strings.IndexByte(first, '/'); idx >= 0 {
		ns := first[:idx]
		svc := strings.TrimSpace(first[idx+1:])
		rest := scopeServices[1:]
		if svc != "" {
			combined := make([]string, 0, 1+len(rest))
			combined = append(combined, svc)
			combined = append(combined, rest...)
			return ns, combined
		}
		return ns, append([]string(nil), rest...)
	}
	return first, append([]string(nil), scopeServices[1:]...)
}

// summarise builds a human-readable summary from a DispatchResult.
//
//	N log lines matched [<logQL>]; first at HH:MM:SS, K distinct pod(s)
//	no log lines matched [<logQL>]
func summarise(dr loki.DispatchResult) string {
	if dr.MatchCount == 0 {
		return fmt.Sprintf("no log lines matched [%s]", dr.LogQL)
	}

	var firstTS time.Time
	pods := make(map[string]struct{})

	sc := bufio.NewScanner(bytes.NewReader(dr.NDJSON))
	for sc.Scan() {
		var rec loki.LogLine
		if jsonErr := json.Unmarshal(sc.Bytes(), &rec); jsonErr != nil {
			continue
		}
		if firstTS.IsZero() {
			if nsInt, parseErr := strconv.ParseInt(rec.TimestampNs, 10, 64); parseErr == nil {
				firstTS = time.Unix(0, nsInt).UTC()
			}
		}
		for _, key := range []string{"pod", "app", "container"} {
			if v := rec.Labels[key]; v != "" {
				pods[v] = struct{}{}
				break
			}
		}
	}

	tsStr := "unknown"
	if !firstTS.IsZero() {
		tsStr = firstTS.Format("15:04:05")
	}
	return fmt.Sprintf(
		"%d log lines matched [%s]; first at %s, %d distinct pod(s)",
		dr.MatchCount, dr.LogQL, tsStr, len(pods),
	)
}

func envOrDefault(key, dflt string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return dflt
}

func main() {
	addr := flag.String("addr", ":8002", "listen address")
	lokiURL := flag.String("loki-url", envOrDefault("LOKI_URL", defaultLokiURL), "Loki base URL")
	bucket := flag.String("bucket", "incidents", "MinIO bucket for evidence blobs")
	flag.Parse()

	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	lokiClient := loki.NewClient(*lokiURL, 30*time.Second)

	var w evidence.Writer
	minioCfg, err := evidence.MinioConfigFromEnv()
	if err != nil {
		logger.Warn("MinIO env vars not set — falling back to NullWriter (no evidence persisted)",
			slog.String("reason", err.Error()))
		w = evidence.NullWriter{Bucket: *bucket}
	} else {
		mw, mwErr := evidence.NewMinioWriter(context.Background(), minioCfg)
		if mwErr != nil {
			logger.Warn("MinioWriter init failed — falling back to NullWriter",
				slog.String("reason", mwErr.Error()),
				slog.String("endpoint", minioCfg.Endpoint),
				slog.String("bucket", minioCfg.Bucket))
			w = evidence.NullWriter{Bucket: *bucket}
		} else {
			logger.Info("MinioWriter active",
				slog.String("endpoint", minioCfg.Endpoint),
				slog.String("bucket", minioCfg.Bucket))
			w = mw
		}
	}

	c := &lokiCollector{writer: w, lokiClient: lokiClient, logger: logger}
	srv := &server.Server{Collector: c, Logger: logger}
	if err := srv.ListenAndServe(*addr); err != nil {
		logger.Error("server exited", slog.String("err", err.Error()))
		os.Exit(1)
	}
}
