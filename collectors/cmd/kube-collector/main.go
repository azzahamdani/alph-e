// Binary kube-collector answers hypothesis-shaped questions against the
// Kubernetes API using a read-only kubeconfig.
//
// Kubeconfig resolution: $KUBECONFIG_AGENT -> $KUBECONFIG -> ~/.kube/config.
// In-cluster auth is never used; a missing or unreadable kubeconfig is
// surfaced as a typed error in the Finding so the Investigator can diagnose it.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"time"

	"k8s.io/client-go/kubernetes"

	"github.com/Matt-LiFi/alph-e/collectors/internal/contract"
	"github.com/Matt-LiFi/alph-e/collectors/internal/evidence"
	"github.com/Matt-LiFi/alph-e/collectors/internal/kube"
	"github.com/Matt-LiFi/alph-e/collectors/internal/server"
)

type kubeCollector struct {
	clientset kubernetes.Interface
	writer    evidence.Writer
	logger    *slog.Logger
}

func (c *kubeCollector) Name() string { return "kube" }

func (c *kubeCollector) Collect(ctx context.Context, in contract.CollectorInput) (contract.CollectorOutput, error) {
	_ = in.EffectiveIterations() // kube dispatch is a single call per question; cap honoured trivially

	dr := kube.Dispatch(ctx, c.clientset, in)

	evidencePayload := dr.RawBody
	if len(evidencePayload) == 0 {
		// Fallback: store the question + scope as a minimal evidence blob so
		// EvidenceRef is never empty.
		evidencePayload = []byte(fmt.Sprintf(`{"question":%q,"scope":%q,"summary":%q}`,
			in.Question, in.ScopeServices, dr.Summary))
	}

	ref, err := c.writer.Put(ctx, evidence.PutRequest{
		IncidentID:  in.IncidentID,
		ContentType: "application/json",
		Payload:     evidencePayload,
	})
	if err != nil {
		return contract.CollectorOutput{}, fmt.Errorf("put evidence: %w", err)
	}

	return contract.CollectorOutput{
		Finding: contract.Finding{
			ID:                 "f_" + ref.EvidenceID,
			CollectorName:      c.Name(),
			Question:           in.Question,
			Summary:            dr.Summary,
			EvidenceID:         ref.EvidenceID,
			Confidence:         dr.Confidence,
			SuggestedFollowups: suggestedFollowups(in, dr),
			CreatedAt:          time.Now().UTC(),
		},
		Evidence:      ref,
		ToolCallsUsed: dr.QueryCount,
	}, nil
}

// suggestedFollowups produces next-step hints the Investigator can use.
func suggestedFollowups(in contract.CollectorInput, dr kube.DispatchResult) []string {
	if dr.Confidence == 0 {
		return []string{
			"verify RBAC permissions: kubectl auth can-i list pods --as=<serviceaccount>",
			"check kubeconfig path in KUBECONFIG_AGENT / KUBECONFIG env vars",
		}
	}
	// Encourage deeper investigation on list-style results.
	if dr.Confidence < 1.0 && len(in.ScopeServices) > 0 {
		ns := in.ScopeServices[0]
		return []string{
			fmt.Sprintf("pod:events %s/<pod-name> -- check per-pod OOM events", ns),
			fmt.Sprintf("deploy:status %s/<name> -- check rollout health", ns),
		}
	}
	return nil
}

func main() {
	addr := flag.String("addr", ":8003", "listen address")
	bucket := flag.String("bucket", "incidents", "MinIO bucket for evidence blobs")
	inCluster := flag.Bool("in-cluster", false, "use the pod's ServiceAccount token instead of a kubeconfig file")
	flag.Parse()

	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))

	// Build clientset. A failure here is not fatal -- we serve a degraded
	// collector that returns the error in every Finding so the Investigator
	// knows to fix the auth config before retrying.
	var (
		cs      kubernetes.Interface
		kubeErr error
	)
	if *inCluster {
		cs, kubeErr = kube.NewInClusterClientset()
	} else {
		cs, kubeErr = kube.NewClientset()
	}
	if kubeErr != nil {
		logger.Warn("kube auth load failed -- collector will surface error in every Finding",
			slog.String("reason", kubeErr.Error()),
			slog.Bool("in_cluster", *inCluster))
	}

	var w evidence.Writer
	minioCfg, err := evidence.MinioConfigFromEnv()
	if err != nil {
		logger.Warn("MinIO env vars not set -- falling back to NullWriter (no evidence persisted)",
			slog.String("reason", err.Error()))
		w = evidence.NullWriter{Bucket: *bucket}
	} else {
		mw, mwErr := evidence.NewMinioWriter(context.Background(), minioCfg)
		if mwErr != nil {
			logger.Warn("MinioWriter init failed -- falling back to NullWriter",
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

	// If kubeconfig failed, wrap a degraded collector that always errors.
	var c server.Collector
	if kubeErr != nil {
		c = &degradedCollector{
			writer:  w,
			logger:  logger,
			kubeErr: kubeErr,
		}
	} else {
		c = &kubeCollector{
			clientset: cs,
			writer:    w,
			logger:    logger,
		}
	}

	srv := &server.Server{Collector: c, Logger: logger}
	if err := srv.ListenAndServe(*addr); err != nil {
		logger.Error("server exited", slog.String("err", err.Error()))
		os.Exit(1)
	}
}

// degradedCollector is used when kubeconfig loading fails at startup. It
// stores a minimal evidence blob and returns a zero-confidence Finding with
// the kubeconfig error in the summary -- surfaced verbatim so the Investigator
// can flag it.
type degradedCollector struct {
	writer  evidence.Writer
	logger  *slog.Logger
	kubeErr error
}

func (c *degradedCollector) Name() string { return "kube" }

func (c *degradedCollector) Collect(ctx context.Context, in contract.CollectorInput) (contract.CollectorOutput, error) {
	summary := fmt.Sprintf("kube-collector unavailable: %s", c.kubeErr.Error())

	payload := []byte(fmt.Sprintf(`{"error":%q}`, c.kubeErr.Error()))
	ref, err := c.writer.Put(ctx, evidence.PutRequest{
		IncidentID:  in.IncidentID,
		ContentType: "application/json",
		Payload:     payload,
	})
	if err != nil {
		return contract.CollectorOutput{}, fmt.Errorf("put evidence: %w", err)
	}

	return contract.CollectorOutput{
		Finding: contract.Finding{
			ID:            "f_" + ref.EvidenceID,
			CollectorName: c.Name(),
			Question:      in.Question,
			Summary:       summary,
			EvidenceID:    ref.EvidenceID,
			Confidence:    0.0,
			SuggestedFollowups: []string{
				"verify KUBECONFIG_AGENT / KUBECONFIG env vars point to a readable kubeconfig",
				"check kubeconfig file permissions and cluster server URL",
			},
			CreatedAt: time.Now().UTC(),
		},
		Evidence: ref,
	}, nil
}
