# ADR-0005: Intake entry point — Alertmanager webhook

- Status: accepted
- Date: 2026-04-21

## Context

The architecture doc assumes Slack is the primary intake (agent receives a support message, parses alert fields, creates a Linear ticket). MVP1 runs entirely on the local lab cluster; we don't want to couple end-to-end testing to a real Slack workspace.

The lab cluster already runs Alertmanager (part of `kube-prometheus-stack`). The leaky-service OOM loop will now fire `PodOOMKilled` (per `monitoring/alert-rules.yaml`). Alertmanager can POST that alert to a webhook — exactly the signal shape the Intake agent needs, without any external dependency.

## Decision

MVP1 intake path:

```
leaky-service OOM → Prometheus fires PodOOMKilled
                 → Alertmanager routes namespace=demo alerts to `receiver: devops-agent`
                 → Alertmanager POSTs JSON to the agent's /webhook/alertmanager endpoint
                 → agent/src/agent/intake/webhook.py parses, seeds IncidentState, enters the graph
```

Host resolution from Alertmanager (in-cluster) to the agent (host-side during MVP1): the k3d loadbalancer resolves the host via **`host.k3d.internal`** (k3d ≥ 5.7). Agent runs on `:8000`, so the webhook URL is `http://host.k3d.internal:8000/webhook/alertmanager`.

Slack adapter deferred. Planned location when it lands: `agent/src/agent/intake/slack.py`. Behind the same `IncidentState` seed — the graph doesn't care where the alert came from.

Alertmanager config lives in `monitoring/kube-prometheus-stack.values.yaml` under `alertmanager.config`. The route matches `namespace="demo"` with `severity="critical"` and sends to `receiver: devops-agent`. Non-matching alerts keep the default route ("`null`" receiver — dropped).

## Consequences

- **+** End-to-end test for MVP1 is fully local: kick the OOM, watch the agent receive a webhook, watch it seed an incident.
- **+** The webhook payload schema is well-known (Alertmanager v4 webhook format) — easy to fixture.
- **+** Real-production pattern for most shops: Alertmanager is where alerts already are, Slack is just a second surface. Building Alertmanager-first is the right long-term shape.
- **−** `host.k3d.internal` is k3d-specific. Docker-compose deployments (if we ever build one for the agent side that also hosts the cluster) would need a different resolution. Documented here; re-evaluate when mixed-deployment lands.
- **−** Slack doesn't exist in MVP1. Linear ticket creation is also deferred (stub raises `NotImplementedError`). These are listed in the MVP1 non-goals in the arch doc, not a gap introduced here.
- The webhook handler validates the AM payload shape and rejects anything else with 400. No authentication in MVP1 (local-only); a shared-secret header added in the first public deployment.
