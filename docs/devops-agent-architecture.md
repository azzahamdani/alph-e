# DevOps Support Agent — Architecture

An agentic system that receives incidents from Slack, diagnoses against cloud infrastructure and the LGTM observability stack, and either opens a PR to fix the issue, executes an operational remediation, or escalates to an on-call engineer with structured context.

Designed around two load-bearing constraints:

1. **Observability data is enormous.** Raw log/trace/metric payloads must never touch agent context. Collectors summarise and reference; raw data lives in an evidence store.
2. **Context sprawl kills agentic systems.** Durable working memory lives in exactly one place (the orchestrator's `IncidentState`). Everything else is ephemeral and stateless, consuming task-local context and returning refined outputs.

---

## Design principles

- **One collector per data source, stateless.** Each invocation is `(question, scope) → (finding, evidence_id)`. No cross-invocation memory, no drift.
- **Evidence store as the shared substrate.** Raw payloads keyed by `evidence_id`. Agents pass references, never blobs.
- **Single source of truth for incident state.** The orchestrator holds `IncidentState`; every other node receives only the slice needed for its current task.
- **Refine, don't retain.** Collectors and subagents burn tokens on heavy local context, then return compressed findings, evidence refs, and next-step signals. The orchestrator carries forward those refined outputs, never raw logs or full prior transcripts.
- **Pre-flight remediation gate.** Not every investigation ends in a PR. A typed `RemediationPlan` decides: `pr`, `rollback`, `scale`, `flag_flip`, `runbook`, or `none`.
- **Separated lifecycle from investigation.** Intake, Investigator, and Coordinator are distinct prompts with distinct responsibilities. Conflating them causes prompt drift.
- **Structured escalation, not silent failure.** When the agent gives up, the human inherits hypotheses, evidence IDs, and attempt history.

---

## System architecture

![system-architecture](diagrams/system-architecture.svg)

---

## Component responsibilities

| Component | Stateful? | Role |
|---|---|---|
| **Intake agent** | no | Parse Slack, extract alert fields, create Linear ticket, bounce if unactionable |
| **Investigator** | yes (owns `IncidentState`) | Hypothesis loop — dispatch collectors, score hypotheses, decide when to hand off |
| **Collectors** | **no (ephemeral)** | One per data source. Answer a single hypothesis-framed question. Write raw to evidence store, return summary. |
| **Validator** | no | Given a candidate hypothesis and evidence refs, confirm or falsify. Runs counter-example searches. |
| **Remediation planner** | no | Decide the remediation *type* before any fix is attempted. Output a typed `RemediationPlan`. |
| **Dev agent** | no | Given a plan of type `pr`, produce a `FixProposal` (branch, diff, commit message, PR body). |
| **Fix verifier** | no | Run `terraform plan` / `helm template` / `kubectl diff` / tests. Classify failures as implementation defects vs diagnosis-invalidating signals. |
| **Coordinator** | no | Post-decision lifecycle: PR creation, reviewer feedback routing, ops execution, Linear updates, escalation. |

The split between Investigator and Coordinator matters: the Investigator's prompt is about *reasoning under uncertainty*; the Coordinator's is about *running a process*. Keeping them separate is what lets each stay narrow and evaluable.

---

## Memory architecture

Four layers, each with a different lifetime and retrieval pattern:

![memory-architecture](diagrams/memory-architecture.svg)

**Key boundaries:**

- Working state is the only memory always in context.
- Working state is composed of refined state: hypotheses, findings, decisions, and evidence references derived from prior subagent runs.
- Working state is checkpointed outside the live model context after every state transition so the orchestrator can resume after crashes, deploys, or long pauses.
- Semantic memory is retrieved per step against the current hypothesis, not pre-loaded.
- Episodic memory is queried once at intake: "have we seen this before?"
- Entity memory is cached per service; cheap to maintain, expensive to recompute per turn.
- Evidence store is durable but TTL-bounded (30 days is usually enough for an incident lifecycle plus postmortem).

---

## State schema

![state-schema](diagrams/state-schema.svg)

**Deliberate choices:**

- `IncidentState` is the only durable container. Collectors receive a `CollectorInput` slice, not the whole state.
- `IncidentState` stores refined outputs from prior steps, not raw observability payloads or full collector transcripts.
- `IncidentState` is persisted as an external checkpoint after each orchestrator step; rehydration rebuilds the live prompt from state plus referenced evidence.
- Checkpoint ordering: evidence-store writes commit *before* the state checkpoint that references them, so rehydration never points at missing blobs. A subagent call in flight at crash time is re-dispatched on restart — idempotency on `(incident_id, collector_name, question, time_range, scope, environment_fingerprint)` makes the retry safe.
- Checkpoint cadence matches the smallest resumable unit — one per orchestrator step in the runtime, one per routing decision in the build fleet. Anything finer-grained wastes I/O; anything coarser loses work on restart.
- `ActionIntent` is the audit unit for mutations: hash, signer, approval record, and execution outcome are part of `IncidentState.actions_taken` alongside the `Action` entry that realised it.
- `EvidenceRef` is a pointer, not content. Raw data lives at `storage_uri`; `expires_at` forces an explicit retention decision.
- `RemediationPlan.type = none` is a valid output — "investigated, nothing to automate, handing off."
- `investigation_attempts` is the single attempt counter, deliberately not duplicated across `FixProposal` or `CollectorInput`.
- `EscalationPackage` is structured so a human inherits context, not just a failure message.

---

## Collector contract

Collectors are pure-ish functions. Each call is a fresh context. The contract is narrow and typed:

```python
# Input
CollectorInput(
    incident_id="inc_2a91",
    question="Is db-primary showing connection errors in the last 15m?",
    hypothesis_id="hyp_3",
    time_range=TimeRange("14:00", "14:15"),
    scope_services=["db-primary"],
    environment_fingerprint=EnvironmentFingerprint(
        cluster="prod-eu-west-1",
        account="123456789012",
        region="eu-west-1",
        deploy_revision="api@v2.14.3",
        rollout_generation="api-7f9a",
    ),
    max_internal_iterations=5,
)

# Output
CollectorOutput(
    finding=Finding(
        collector_name="loki",
        summary="847 'connection refused' errors, onset 14:02:17, affecting 3 upstream services",
        evidence_id="ev_8f2a",
        confidence=0.92,
        suggested_followups=[
            "check db-primary pod status",
            "correlate with deploys in window",
        ],
    ),
    evidence=EvidenceRef(
        evidence_id="ev_8f2a",
        storage_uri="s3://incidents/ev_8f2a.jsonl",
        content_type="application/x-ndjson",
        size_bytes=4_821_334,
        expires_at="2026-05-21T00:00:00Z",
    ),
    tool_calls_used=3,
    tokens_used=1_842,
)
```

**Why `max_internal_iterations`:** collectors are allowed to iterate (run 2–3 refined queries before returning) because good log triage genuinely needs it. But the cap is explicit — without it, a collector can quietly blow up its own context window chasing a dead hypothesis. Five is usually enough; hard stop.

**Caching:** collector calls are memoised on `(incident_id, collector_name, question, time_range, scope, environment_fingerprint)`. Five-minute TTL. `environment_fingerprint` should capture the cluster / account / region plus freshness signals like deploy revision or rollout generation so cached findings are invalidated when the system materially changes.

---

## Remediation decision

The pre-flight gate that prevents the system from forcing PRs when the right action is operational:

![remediation-decision](diagrams/remediation-decision.svg)

**Rules:**

- Only `type = pr` goes to the Dev agent.
- Operational actions (`rollback`, `scale`, `flag_flip`, `runbook`) go to the Coordinator and execute against cloud/k8s APIs with full audit logging in `IncidentState.actions_taken`.
- `requires_human_approval=True` on the plan forces a Slack confirmation before the Coordinator acts — default `True` for anything that mutates production.
- `type = none` is a clean escalation path when the agent has investigated but has no actionable remediation.

**Safety contract for operational actions:**

- Every mutable action is materialised as a signed `ActionIntent` with a stable hash over target, parameters, expected effect, rollback hint, and expiry.
- The **Planner** signs the intent; the **Coordinator** verifies the signature before executing. Keys are held by distinct identities so a compromised Coordinator cannot forge intents.
- Human approval binds to that exact `ActionIntent` hash; if the plan changes, approval is invalidated (`approval_status = invalidated`) and must be re-issued.
- Default approval validity: 15 minutes from grant, or until the next precondition check fails — whichever is sooner. `ActionIntent.expires_at` encodes the ceiling.
- Coordinator executions use `ActionIntent.hash` as the idempotency key so retries cannot fan out duplicate mutations.
- Mutations run under a dedicated least-privilege service identity, never a human engineer's ambient credentials.
- Immediately before execution the Coordinator re-checks preconditions against live state. Stale plans are rejected and routed by *what changed*:
  - **Diagnosis-invalidating change** (the root-cause observation no longer holds) → Investigator.
  - **Parameter-only change** (target pod renamed, replica count already adjusted) → Planner for a fresh `ActionIntent`.
  - **Already-resolved** (the bad deploy was rolled back by a human, the flag is already flipped) → Coordinator short-circuits to `resolved` with a `no_op` action record; no round-trip.
- Partial success triggers compensation: the Coordinator executes the inverse derived from `ActionIntent.rollback_hint`, records both the forward and compensating actions in `IncidentState.actions_taken`, and escalates. No unbounded self-healing loops.

---

## Escalation path

Escalation is a **handoff**, not a failure. The on-call engineer receives a structured package and the agent stays available as a tool.

![escalation-sequence](diagrams/escalation-sequence.svg)

**What the EscalationPackage contains** (not "I failed 3 times"):

- Hypotheses considered, with scores and current status.
- Key findings with evidence IDs (linkable to Grafana/Loki dashboards).
- Attempts made, each with its rejection reason.
- Current working theory and confidence.
- Suggested next steps the agent couldn't execute itself (missing permissions, genuine ambiguity, needs human judgement).

---

## Subagents and token management

Investigation phases that generate a lot of tokens internally but only need to return a conclusion run as **subagents with their own context windows**. The orchestrator sends a task seeded from the current refined incident state, the subagent burns tokens on heavy local context, and returns only a structured finding.

![subagents-dispatch](diagrams/subagents-dispatch.svg)

**Cost tactics:**

| Tactic | Impact |
|---|---|
| Prompt caching on system prompt, tool defs, service catalog | ~90% discount on the stable prefix — largest single lever |
| Tiered models: small for orchestrator routing, larger for hypothesis synthesis | Reserves expensive tokens for hard reasoning |
| Parallel collector dispatch when hypotheses are independent | Latency win; no token penalty |
| Server-side aggregation (LogQL counts, PromQL rates) instead of raw data | Push computation to data plane, not the LLM |
| Sliding window + rolling summary on long incidents | Past turns compressed into "what we've learned so far" paragraph |
| Collector output cache, 5-min TTL, keyed on incident + scope + environment fingerprint | Free for re-framings within the same incident without serving stale cross-environment results |

---

## Key routing decisions

A few non-obvious edges worth calling out because they're the ones that typically get wired wrong:

| From | Condition | To | Why |
|---|---|---|---|
| Reviewer | changes on the **fix** | Dev agent | Don't re-investigate for a typo fix |
| Reviewer | challenges the **root cause** | Investigator | Full rethink warranted |
| Verifier | implementation defect (`VerifierResult.kind = implementation_error`) | Dev agent | Diagnosis still stands; patch the fix |
| Verifier | diagnosis invalidated (`VerifierResult.kind = diagnosis_invalidated`) | Investigator | Dry-run evidence contradicted the current root-cause theory |
| Planner | `type = none` | Coordinator → escalation | Not a failure; a legitimate outcome |
| Investigator | attempts exhausted | Coordinator (not directly to Slack) | Coordinator owns lifecycle; escalation is a lifecycle event |
| On-call | follow-up question | Investigator | Agent stays available during handoff |

---

## Failure modes to instrument

The happy path is easy. These are the paths that determine whether the system actually works in practice:

- **Collector returns empty / no signal.** Does the Investigator correctly update hypothesis status, or does it loop?
- **Hypotheses all score below threshold.** Does the system correctly hand off as `type = none` rather than forcing a weak PR?
- **Verifier fails repeatedly on the same proposal.** Cap Dev → Verifier iterations at 3; escalate beyond.
- **Verifier invalidates the diagnosis.** The verifier must be able to reopen investigation, not just request implementation tweaks.
- **Reviewer requests changes ambiguously.** Default to "changes on fix" route; only re-open investigation on explicit challenge to root cause.
- **Orchestrator asks bad collector questions.** "Show me logs from db-primary" is a UI query, not a hypothesis test. Invest in an eval set of past incidents to check question quality.
- **Evidence store GC-ing mid-incident.** Lifecycle rules must be longer than expected incident duration + postmortem window.
- **Approved action executes against changed reality.** Re-check preconditions immediately before mutation; default approval validity 15 min or until a precondition re-check fails.
- **Already-resolved race.** Fresh precondition check reveals the bad deploy was already rolled back by a human — Coordinator must short-circuit to `resolved` rather than round-trip through Investigator/Planner.
- **Crashed with subagent in flight.** State checkpoint must commit *after* evidence writes, and subagent dispatch must be keyed on `(incident_id, collector_name, question, time_range, scope, environment_fingerprint)` so restart re-dispatch is idempotent.

---

## Open decisions

Things the architecture deliberately leaves to you:

1. **Collector iteration model.** Single-shot per call, or iterative with a cap? Recommend iterative with `max_internal_iterations=5`. More powerful for log triage; still bounded.
2. **Human-in-the-loop mode for escalation.** Does the agent stay active as a tool, or is escalation a full handoff? Recommend the former — keeps institutional memory.
3. **Auto-execute vs. auto-propose for operational actions.** `rollback` and `scale` can be safe to auto-execute; `flag_flip` often needs approval. Default `requires_human_approval=True`; relax per action type with deliberation.
4. **Which model tier per role.** MVP1 uses Claude Sonnet everywhere to keep the PoC simple and measurable. Post-MVP: Haiku for Intake and Coordinator routing, Sonnet for Investigator and most collectors, Opus only for hard hypothesis synthesis. Tiering only after there's a baseline to compare against.
5. **Episodic memory bootstrap.** Does the system start cold, or seed from existing postmortems? Seeding is worth the one-time ETL cost — past incidents are where the agent gets smarter.

---

## Suggested stack

| Layer | Choice | Notes |
|---|---|---|
| Agent framework | PydanticAI or LangGraph | Both have first-class typed state; LangGraph is better at explicit graph control flow |
| LLM | Anthropic Claude Sonnet (single tier for MVP1) | Single model across all roles until there's a baseline to tier against; prompt caching is the killer cost lever regardless |
| Semantic + episodic memory | Postgres + pgvector | Simple, adequate; don't over-engineer |
| Evidence store | S3 / MinIO (blobs) + Postgres (metadata) | 30-day lifecycle rules |
| Cloud / k8s tools | MCP servers per provider | Composable, swappable, auditable |
| Observability tools | MCP servers for Grafana / Loki / Tempo / Prometheus | Same pattern |
| Code / PR | GitHub API + Claude Code CLI (optional) | Claude Code handles branch / commit / PR mechanics well |
| Ticketing | Linear API | Updates at intake, planning, PR open, resolution, escalation |
| Reference to study | HolmesGPT (Robusta) | Good prior art for k8s + observability diagnosis; steal the runbook retrieval pattern |

---

## MVP1: PoC deployment

MVP1 is a proof-of-concept on a small demo cluster. The goal is to exercise the full graph end-to-end against canned incidents and validate the architecture's load-bearing claims (bounded context, typed contracts, evidence-by-reference, pre-flight gates). It is not production and is not tuned for cost or latency.

### Model selection

**Claude Sonnet for every role.** One model across Intake, Investigator, collectors, Planner, Dev, Verifier, and Coordinator. Rationale:

- Sonnet is cheap enough per token that the PoC's end-to-end spend is bounded, and smart enough that no role is the weak link.
- Avoids the multi-model variance problem — if a PoC misfires, we know it's the prompt or the graph, not a model mismatch.
- Prompt caching applies to the stable system/tool prefixes across all roles, which is the real cost lever.
- Tiering to Haiku on routing roles (Intake, Coordinator) and Opus on synthesis (hard hypothesis work) is a post-MVP optimisation — requires an MVP1 baseline to measure against.

No local inference in MVP1. Explicit non-goal.

### Demo cluster

A small Kubernetes cluster (kind / k3d / minikube, or a single-node EKS/GKE dev cluster) running:

- 2–3 toy services with intentionally breakable failure modes (OOM, bad deploy, dependency timeout).
- Grafana LGTM stack or the Grafana Cloud free tier as the observability backend.
- A seed incident generator that triggers each failure mode on demand.

The agent runs alongside as a container; everything talks to the demo cluster's APIs.

### Docker compose (agent side)

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    container_name: postgres
    environment:
      POSTGRES_PASSWORD: devops
      POSTGRES_DB: incidents
    volumes:
      - postgres-data:/var/lib/postgresql/data
    ports:
      - "5432:5432"

  minio:
    image: minio/minio:latest
    container_name: minio
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: minio
      MINIO_ROOT_PASSWORD: minio-dev-secret
    volumes:
      - minio-data:/data
    ports:
      - "9000:9000"
      - "9001:9001"

  devops-agent:
    build: ./agent
    container_name: devops-agent
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - MODEL_NAME=claude-sonnet-4-6
      - POSTGRES_URL=postgresql://postgres:devops@postgres:5432/incidents
      - EVIDENCE_S3_ENDPOINT=http://minio:9000
      - EVIDENCE_S3_BUCKET=incidents
      - KUBECONFIG=/root/.kube/config
    volumes:
      - ~/.kube:/root/.kube:ro
    depends_on:
      - postgres
      - minio

volumes:
  postgres-data:
  minio-data:
```

### What MVP1 deliberately does *not* cover

- **No local inference.** Addressed post-MVP if token spend on real incident volume justifies the ops burden.
- **No model tiering.** Sonnet-everywhere until there's data showing where tiering pays off.
- **No production mutations.** `requires_human_approval=True` on every `ActionIntent`; the Coordinator's cloud-mutation paths are stubbed to `dry_run` against the demo cluster.
- **Single-cluster, single-environment.** `environment_fingerprint` still encodes cluster/region/revision, but there's only one environment, so cross-environment cache poisoning can't be exercised until MVP2.
- **Episodic memory cold-start.** No postmortem ETL in MVP1 — the system learns from the canned incident set only.

### Post-MVP: mixed deployment

Once the PoC validates the graph, the practical production pattern is **local collectors, cloud orchestrator**:

![mixed-deployment](diagrams/mixed-deployment.svg)

Collectors are high-volume and mechanical — they benefit from being local (no per-call cost, low latency, data never leaves the VPC). The orchestrator and Dev agent are low-volume but high-stakes — they benefit from frontier-model reasoning. Typically cuts cloud spend 70%+ versus all-cloud while keeping hard-reasoning quality where it matters. Config surface lives on `IncidentState.phase` so model binding is declarative, not hardcoded into prompts.

Not in scope for MVP1.

---

## Related documents

- **`devops-agent-build-fleet.md`** — companion doc describing a fleet of specialist agents that constructs this runtime system. Mirrors the same patterns (narrow ephemeral specialists, stateful orchestrator, typed work-item contracts, verifier loop). Useful if you're building with Claude Code subagents, Archon Agent Work Orders, or a custom multi-agent build pipeline.
