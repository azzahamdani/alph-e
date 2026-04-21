package kube_test

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/Matt-LiFi/alph-e/collectors/internal/kube"
)

// TestNewClientset_MissingKubeconfig verifies that NewClientset returns a
// *KubeconfigError (never a panic or silent in-cluster fallback) when no
// kubeconfig can be found.
//
// We clear all three env vars and point the default path to a non-existent
// file by overriding HOME.
func TestNewClientset_MissingKubeconfig(t *testing.T) {
	// Isolate env so we don't accidentally pick up the developer's real config.
	t.Setenv("KUBECONFIG_AGENT", "")
	t.Setenv("KUBECONFIG", "")

	// Point HOME at an empty temp dir so ~/.kube/config does not exist.
	tmpHome := t.TempDir()
	t.Setenv("HOME", tmpHome)

	_, err := kube.NewClientset()
	require.Error(t, err)

	var kubecfgErr *kube.KubeconfigError
	assert.ErrorAs(t, err, &kubecfgErr,
		"NewClientset must return *KubeconfigError when no kubeconfig is found")
}

// TestNewClientset_KUBECONFIG_AGENT_Wins verifies env priority: when
// KUBECONFIG_AGENT is set it beats KUBECONFIG.  We point it at a clearly
// invalid path so we can assert the error message names the right source.
func TestNewClientset_KUBECONFIG_AGENT_Wins(t *testing.T) {
	bogus := "/non/existent/kubeconfig"
	t.Setenv("KUBECONFIG_AGENT", bogus)
	t.Setenv("KUBECONFIG", "/should/not/be/read")

	_, err := kube.NewClientset()
	require.Error(t, err)

	var kubecfgErr *kube.KubeconfigError
	require.ErrorAs(t, err, &kubecfgErr)
	assert.Equal(t, "KUBECONFIG_AGENT", kubecfgErr.Source)
	assert.Equal(t, bogus, kubecfgErr.Path)
}

// TestNewClientset_KUBECONFIG_FallsBack verifies that KUBECONFIG is used when
// KUBECONFIG_AGENT is unset.
func TestNewClientset_KUBECONFIG_FallsBack(t *testing.T) {
	bogus := "/non/existent/kubeconfig-fallback"
	t.Setenv("KUBECONFIG_AGENT", "")
	t.Setenv("KUBECONFIG", bogus)

	_, err := kube.NewClientset()
	require.Error(t, err)

	var kubecfgErr *kube.KubeconfigError
	require.ErrorAs(t, err, &kubecfgErr)
	assert.Equal(t, "KUBECONFIG", kubecfgErr.Source)
	assert.Equal(t, bogus, kubecfgErr.Path)
}

// TestNewClientset_DefaultPath verifies the default path fallback uses
// ~/.kube/config (rendered as <HOME>/.kube/config) when neither env var is set.
func TestNewClientset_DefaultPath(t *testing.T) {
	t.Setenv("KUBECONFIG_AGENT", "")
	t.Setenv("KUBECONFIG", "")

	tmpHome := t.TempDir()
	t.Setenv("HOME", tmpHome)

	_, err := kube.NewClientset()
	require.Error(t, err)

	var kubecfgErr *kube.KubeconfigError
	require.ErrorAs(t, err, &kubecfgErr)
	assert.Equal(t, "default", kubecfgErr.Source)
	// Path must include the temp home dir, not some other location.
	assert.Contains(t, kubecfgErr.Path, tmpHome)
}
