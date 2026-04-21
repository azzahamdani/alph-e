// Package kube wraps client-go into the minimal read-only surface the
// kube-collector needs.  It never issues write verbs (create/update/patch/
// delete); any such call is a bug.
//
// Kubeconfig resolution order (matches kubectl):
//  1. $KUBECONFIG_AGENT  — preferred; lets the agent run with a constrained
//     service-account kubeconfig distinct from the operator's own.
//  2. $KUBECONFIG        — standard kubectl env override.
//  3. ~/.kube/config     — default location.
//
// In-cluster auth is deliberately excluded — the collector always needs a
// named kubeconfig so the Investigator can reason about which cluster is
// being queried.
package kube

import (
	"fmt"
	"os"
	"path/filepath"

	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/clientcmd"
)

// KubeconfigError is returned when kubeconfig loading fails.  It wraps the
// underlying error so callers can distinguish a configuration problem from a
// network / RBAC problem.
type KubeconfigError struct {
	Path   string
	Source string // "KUBECONFIG_AGENT" | "KUBECONFIG" | "default"
	Cause  error
}

func (e *KubeconfigError) Error() string {
	return fmt.Sprintf("kube: load kubeconfig from %s (%s): %v", e.Source, e.Path, e.Cause)
}

func (e *KubeconfigError) Unwrap() error { return e.Cause }

// resolveKubeconfigPath returns (path, source) following the priority order
// defined in the package doc.  It returns an error if none of the sources
// yield a usable path.
func resolveKubeconfigPath() (path, source string, err error) {
	if v := os.Getenv("KUBECONFIG_AGENT"); v != "" {
		return v, "KUBECONFIG_AGENT", nil
	}
	if v := os.Getenv("KUBECONFIG"); v != "" {
		return v, "KUBECONFIG", nil
	}

	home, err := os.UserHomeDir()
	if err != nil {
		return "", "default", fmt.Errorf("resolve home dir: %w", err)
	}
	defaultPath := filepath.Join(home, ".kube", "config")
	return defaultPath, "default", nil
}

// NewClientset builds a kubernetes.Interface from whichever kubeconfig source
// wins.  It bails with *KubeconfigError on any failure; callers should surface
// the message verbatim in the Finding so the Investigator can diagnose
// misconfiguration.
func NewClientset() (kubernetes.Interface, error) {
	kubeconfigPath, source, err := resolveKubeconfigPath()
	if err != nil {
		return nil, &KubeconfigError{Source: source, Cause: err}
	}

	loadingRules := &clientcmd.ClientConfigLoadingRules{
		ExplicitPath: kubeconfigPath,
	}
	clientConfig := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(
		loadingRules,
		&clientcmd.ConfigOverrides{},
	)

	restCfg, err := clientConfig.ClientConfig()
	if err != nil {
		return nil, &KubeconfigError{
			Path:   kubeconfigPath,
			Source: source,
			Cause:  err,
		}
	}

	cs, err := kubernetes.NewForConfig(restCfg)
	if err != nil {
		return nil, &KubeconfigError{
			Path:   kubeconfigPath,
			Source: source,
			Cause:  fmt.Errorf("build clientset: %w", err),
		}
	}
	return cs, nil
}
