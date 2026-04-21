// Binary prom-collector answers hypothesis-shaped questions against Prometheus.
//
// MVP1 scope: this is a skeleton. The collector method returns a placeholder
// Finding with confidence 0.0 and a summary describing what it *would* have
// queried. Real PromQL dispatch lands in WI-005.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"time"

	"github.com/Matt-LiFi/alph-e/collectors/internal/contract"
	"github.com/Matt-LiFi/alph-e/collectors/internal/evidence"
	"github.com/Matt-LiFi/alph-e/collectors/internal/server"
)

type promCollector struct {
	writer evidence.Writer
	logger *slog.Logger
}

func (c *promCollector) Name() string { return "prom" }

func (c *promCollector) Collect(ctx context.Context, in contract.CollectorInput) (contract.CollectorOutput, error) {
	ref, err := c.writer.Put(ctx, evidence.PutRequest{
		IncidentID:  in.IncidentID,
		ContentType: "application/json",
		Payload:     []byte(fmt.Sprintf(`{"question":%q,"scope":%q}`, in.Question, in.ScopeServices)),
	})
	if err != nil {
		return contract.CollectorOutput{}, fmt.Errorf("put evidence: %w", err)
	}
	finding := contract.Finding{
		ID:            "f_" + ref.EvidenceID,
		CollectorName: c.Name(),
		Question:      in.Question,
		Summary: fmt.Sprintf(
			"SKELETON: would PromQL-query %v over [%s, %s] on cluster=%s",
			in.ScopeServices,
			in.TimeRange.Start.Format(time.RFC3339),
			in.TimeRange.End.Format(time.RFC3339),
			in.EnvironmentFingerprint.Cluster,
		),
		EvidenceID: ref.EvidenceID,
		Confidence: 0.0,
		SuggestedFollowups: []string{
			"replace skeleton with PromQL dispatch (WI-005)",
		},
		CreatedAt: time.Now().UTC(),
	}
	return contract.CollectorOutput{Finding: finding, Evidence: ref}, nil
}

func main() {
	addr := flag.String("addr", ":8001", "listen address")
	bucket := flag.String("bucket", "incidents", "MinIO bucket for evidence blobs")
	flag.Parse()

	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))

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

	c := &promCollector{
		writer: w,
		logger: logger,
	}
	srv := &server.Server{Collector: c, Logger: logger}
	if err := srv.ListenAndServe(*addr); err != nil {
		logger.Error("server exited", slog.String("err", err.Error()))
		os.Exit(1)
	}
}
