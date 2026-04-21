---
id: C-04
subject: kube-collector — real client-go dispatch (read-only)
track: gamma
depends_on: [C-01]
advances_wi: [WI-007]
---

## Goal

Replace the SKELETON in `cmd/kube-collector/main.go` with real
`k8s.io/client-go` reads against the lab cluster using a read-only
kubeconfig.

## Requirements

- Load kubeconfig via `clientcmd.NewNonInteractiveDeferredLoadingClientConfig`
  honouring `KUBECONFIG_AGENT` (preferred) then `KUBECONFIG` then
  `~/.kube/config`. Bail on first failure with a typed error — never silently
  fall back to in-cluster auth.
- Question patterns for MVP1:
  - `pod:status <namespace>` → list pods, return phase + recent restart count.
  - `pod:events <namespace>/<name>` → list events sorted by lastTimestamp.
  - `deploy:status <namespace>/<name>` → desired vs ready replicas + rollout
    condition.
  - `default` → list pods in `scope_services[0]` namespace whose `lastState`
    is `Terminated`.
- Persist the raw JSON payload of the API response as the evidence blob.
- Confidence: `1.0` if the question maps to a single deterministic answer,
  `0.5` for list-style outputs.

## Deliverables

- `collectors/internal/kube/client.go` — wraps `kubernetes.Interface`.
- `collectors/internal/kube/dispatch.go`.
- Replace `cmd/kube-collector/main.go::kubeCollector.Collect`.
- Tests with `fake.NewSimpleClientset()`.

## Acceptance

- Lint + race tests clean.
- Integration test against the lab k3d cluster returns the demo namespace's
  pod list and at least one OOMKilled event for the leaky pod.

## Guardrails

- Read-only verbs only (`get`, `list`, `watch`). Any write attempt is a bug.
- Surface RBAC errors verbatim — the Investigator must be able to suggest
  "ask for missing permission".

## Done signal

Flip `C-04` in [`../dependencies.md`](../dependencies.md) to `done`.
