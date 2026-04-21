// Binary prom-collector answers hypothesis-shaped questions against Prometheus.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/Matt-LiFi/alph-e/collectors/internal/contract"
	"github.com/Matt-LiFi/alph-e/collectors/internal/evidence"
	"github.com/Matt-LiFi/alph-e/collectors/internal/prom"
	"github.com/Matt-LiFi/alph-e/collectors/internal/server"
)

const defaultPrometheusURL = "http://prometheus.monitoring.svc.cluster.local:9090"

type promCollector struct {
	writer     evidence.Writer
	promClient *prom.Client
	logger     *slog.Logger
}

func (c *promCollector) Name() string { return "prom" }

//nolint:gocritic // hugeParam: CollectorInput is fixed by the server.Collector interface contract
func (c *promCollector) Collect(ctx context.Context, in contract.CollectorInput) (contract.CollectorOutput, error) {
	maxIter := in.EffectiveIterations()

	// Divide the context deadline evenly across iterations.  The server already
	// sets a 30s overall deadline; we honour it rather than imposing our own.
	iterTimeout := remainingTime(ctx) / time.Duration(maxIter)
	if iterTimeout < time.Second {
		iterTimeout = time.Second
	}

	iterCtx, cancel := context.WithTimeout(ctx, iterTimeout)
	defer cancel()

	dr := prom.Dispatch(iterCtx, c.promClient, &in, maxIter)

	payload := dr.RawBody
	if len(payload) == 0 {
		payload = []byte(`{}`)
	}

	ref, err := c.writer.Put(ctx, evidence.PutRequest{
		IncidentID:  in.IncidentID,
		ContentType: "application/json",
		Payload:     payload,
	})
	if err != nil {
		return contract.CollectorOutput{}, fmt.Errorf("put evidence: %w", err)
	}

	finding := contract.Finding{
		ID:            "f_" + ref.EvidenceID,
		CollectorName: c.Name(),
		Question:      in.Question,
		Summary:       dr.Summary,
		EvidenceID:    ref.EvidenceID,
		Confidence:    dr.Confidence,
		CreatedAt:     time.Now().UTC(),
	}

	return contract.CollectorOutput{
		Finding:       finding,
		Evidence:      ref,
		ToolCallsUsed: dr.QueryCount,
	}, nil
}

// remainingTime returns the time left in ctx, or 30s if no deadline is set.
func remainingTime(ctx context.Context) time.Duration {
	dl, ok := ctx.Deadline()
	if !ok {
		return 30 * time.Second
	}
	d := time.Until(dl)
	if d <= 0 {
		return time.Second
	}
	return d
}

func main() {
	addr := flag.String("addr", ":8001", "listen address")
	bucket := flag.String("bucket", "incidents", "MinIO bucket for evidence blobs")
	flag.Parse()

	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))

	// Evidence writer — prefer MinIO, fall back to NullWriter for local dev.
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

	// Prometheus client — URL from env, defaulting to in-cluster address.
	promURL := os.Getenv("PROMETHEUS_URL")
	if promURL == "" {
		promURL = defaultPrometheusURL
	}
	logger.Info("Prometheus target", slog.String("url", promURL))
	promClient := prom.NewClient(promURL, &http.Client{Timeout: 25 * time.Second})

	c := &promCollector{
		writer:     w,
		promClient: promClient,
		logger:     logger,
	}
	srv := &server.Server{Collector: c, Logger: logger}
	if err := srv.ListenAndServe(*addr); err != nil {
		logger.Error("server exited", slog.String("err", err.Error()))
		os.Exit(1)
	}
}
