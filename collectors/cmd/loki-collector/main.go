// Binary loki-collector answers hypothesis-shaped questions against Loki.
//
// MVP1 skeleton: same pattern as prom-collector. Real LogQL dispatch lands
// in WI-006.
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

type lokiCollector struct {
	writer evidence.Writer
	logger *slog.Logger
}

func (c *lokiCollector) Name() string { return "loki" }

func (c *lokiCollector) Collect(ctx context.Context, in contract.CollectorInput) (contract.CollectorOutput, error) {
	ref, err := c.writer.Put(ctx, evidence.PutRequest{
		IncidentID:  in.IncidentID,
		ContentType: "application/x-ndjson",
		Payload:     []byte(fmt.Sprintf(`{"question":%q,"scope":%q}`, in.Question, in.ScopeServices)),
	})
	if err != nil {
		return contract.CollectorOutput{}, fmt.Errorf("put evidence: %w", err)
	}
	return contract.CollectorOutput{
		Finding: contract.Finding{
			ID:            "f_" + ref.EvidenceID,
			CollectorName: c.Name(),
			Question:      in.Question,
			Summary: fmt.Sprintf(
				"SKELETON: would LogQL-query %v over [%s, %s]",
				in.ScopeServices,
				in.TimeRange.Start.Format(time.RFC3339),
				in.TimeRange.End.Format(time.RFC3339),
			),
			EvidenceID: ref.EvidenceID,
			Confidence: 0.0,
			SuggestedFollowups: []string{
				"replace skeleton with LogQL dispatch (WI-006)",
			},
			CreatedAt: time.Now().UTC(),
		},
		Evidence: ref,
	}, nil
}

func main() {
	addr := flag.String("addr", ":8002", "listen address")
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

	c := &lokiCollector{writer: w, logger: logger}
	srv := &server.Server{Collector: c, Logger: logger}
	if err := srv.ListenAndServe(*addr); err != nil {
		logger.Error("server exited", slog.String("err", err.Error()))
		os.Exit(1)
	}
}
