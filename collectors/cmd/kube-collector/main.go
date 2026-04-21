// Binary kube-collector answers hypothesis-shaped questions against the
// Kubernetes API using a read-only kubeconfig.
//
// MVP1 skeleton: no client-go wiring yet. Real dispatch lands in WI-007.
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

type kubeCollector struct {
	writer evidence.Writer
	logger *slog.Logger
}

func (c *kubeCollector) Name() string { return "kube" }

func (c *kubeCollector) Collect(ctx context.Context, in contract.CollectorInput) (contract.CollectorOutput, error) {
	ref, err := c.writer.Put(ctx, evidence.PutRequest{
		IncidentID:  in.IncidentID,
		ContentType: "application/json",
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
				"SKELETON: would describe pods in %v on cluster=%s",
				in.ScopeServices,
				in.EnvironmentFingerprint.Cluster,
			),
			EvidenceID: ref.EvidenceID,
			Confidence: 0.0,
			SuggestedFollowups: []string{
				"replace skeleton with client-go dispatch (WI-007)",
			},
			CreatedAt: time.Now().UTC(),
		},
		Evidence: ref,
	}, nil
}

func main() {
	addr := flag.String("addr", ":8003", "listen address")
	bucket := flag.String("bucket", "incidents", "MinIO bucket for evidence blobs")
	flag.Parse()

	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	c := &kubeCollector{writer: evidence.NullWriter{Bucket: *bucket}, logger: logger}
	srv := &server.Server{Collector: c, Logger: logger}
	if err := srv.ListenAndServe(*addr); err != nil {
		logger.Error("server exited", slog.String("err", err.Error()))
		os.Exit(1)
	}
}
