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
| **Fix verifier** | no | Run `terraform plan` / `helm template` / `kubectl diff` / tests. Loop back to Dev on failure. |
| **Coordinator** | no | Post-decision lifecycle: PR creation, reviewer feedback routing, ops execution, Linear updates, escalation. |

The split between Investigator and Coordinator matters: the Investigator's prompt is about *reasoning under uncertainty*; the Coordinator's is about *running a process*. Keeping them separate is what lets each stay narrow and evaluable.

---

## Memory architecture

Four layers, each with a different lifetime and retrieval pattern:

![memory-architecture](diagrams/memory-architecture.svg)

**Key boundaries:**

- Working state is the only memory always in context.
- Working state is composed of refined state: hypotheses, findings, decisions, and evidence references derived from prior subagent runs.
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
    question="Is db-primary showing connection errors in the last 15m?",
    hypothesis_id="hyp_3",
    time_range=TimeRange("14:00", "14:15"),
    scope_services=["db-primary"],
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

**Caching:** collector calls are memoised on `(collector_name, question, time_range, scope)`. Five-minute TTL. During an incident the orchestrator often re-frames the same window; no reason to re-hit Loki.

---

## Remediation decision

The pre-flight gate that prevents the system from forcing PRs when the right action is operational:

![remediation-decision](diagrams/remediation-decision.svg)

**Rules:**

- Only `type = pr` goes to the Dev agent.
- Operational actions (`rollback`, `scale`, `flag_flip`, `runbook`) go to the Coordinator and execute against cloud/k8s APIs with full audit logging in `IncidentState.actions_taken`.
- `requires_human_approval=True` on the plan forces a Slack confirmation before the Coordinator acts — default `True` for anything that mutates production.
- `type = none` is a clean escalation path when the agent has investigated but has no actionable remediation.

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
| Collector output cache, 5-min TTL, keyed on `(query, time_range)` | Free for re-framings within the same incident |

---

## Key routing decisions

A few non-obvious edges worth calling out because they're the ones that typically get wired wrong:

| From | Condition | To | Why |
|---|---|---|---|
| Reviewer | changes on the **fix** | Dev agent | Don't re-investigate for a typo fix |
| Reviewer | challenges the **root cause** | Investigator | Full rethink warranted |
| Verifier | dry-run fails | Dev agent | Not the Investigator — the diagnosis is still valid |
| Planner | `type = none` | Coordinator → escalation | Not a failure; a legitimate outcome |
| Investigator | attempts exhausted | Coordinator (not directly to Slack) | Coordinator owns lifecycle; escalation is a lifecycle event |
| On-call | follow-up question | Investigator | Agent stays available during handoff |

---

## Failure modes to instrument

The happy path is easy. These are the paths that determine whether the system actually works in practice:

- **Collector returns empty / no signal.** Does the Investigator correctly update hypothesis status, or does it loop?
- **Hypotheses all score below threshold.** Does the system correctly hand off as `type = none` rather than forcing a weak PR?
- **Verifier fails repeatedly on the same proposal.** Cap Dev → Verifier iterations at 3; escalate beyond.
- **Reviewer requests changes ambiguously.** Default to "changes on fix" route; only re-open investigation on explicit challenge to root cause.
- **Orchestrator asks bad collector questions.** "Show me logs from db-primary" is a UI query, not a hypothesis test. Invest in an eval set of past incidents to check question quality.
- **Evidence store GC-ing mid-incident.** Lifecycle rules must be longer than expected incident duration + postmortem window.

---

## Open decisions

Things the architecture deliberately leaves to you:

1. **Collector iteration model.** Single-shot per call, or iterative with a cap? Recommend iterative with `max_internal_iterations=5`. More powerful for log triage; still bounded.
2. **Human-in-the-loop mode for escalation.** Does the agent stay active as a tool, or is escalation a full handoff? Recommend the former — keeps institutional memory.
3. **Auto-execute vs. auto-propose for operational actions.** `rollback` and `scale` can be safe to auto-execute; `flag_flip` often needs approval. Default `requires_human_approval=True`; relax per action type with deliberation.
4. **Which model tier per role.** Haiku for Intake and Coordinator routing; Sonnet for Investigator and most collectors; Opus only for hard hypothesis synthesis. Measure before locking in.
5. **Episodic memory bootstrap.** Does the system start cold, or seed from existing postmortems? Seeding is worth the one-time ETL cost — past incidents are where the agent gets smarter.

---

## Suggested stack

| Layer | Choice | Notes |
|---|---|---|
| Agent framework | PydanticAI or LangGraph | Both have first-class typed state; LangGraph is better at explicit graph control flow |
| LLM | Anthropic (Claude Sonnet + Haiku mix) | Prompt caching is the killer cost lever |
| Semantic + episodic memory | Postgres + pgvector | Simple, adequate; don't over-engineer |
| Evidence store | S3 / MinIO (blobs) + Postgres (metadata) | 30-day lifecycle rules |
| Cloud / k8s tools | MCP servers per provider | Composable, swappable, auditable |
| Observability tools | MCP servers for Grafana / Loki / Tempo / Prometheus | Same pattern |
| Code / PR | GitHub API + Claude Code CLI (optional) | Claude Code handles branch / commit / PR mechanics well |
| Ticketing | Linear API | Updates at intake, planning, PR open, resolution, escalation |
| Reference to study | HolmesGPT (Robusta) | Good prior art for k8s + observability diagnosis; steal the runbook retrieval pattern |

---

## Local demo deployment

For a self-contained demo that runs on a 16GB box (16GB VRAM or 16GB unified memory on Apple Silicon). Not production; enough to exercise the full graph end-to-end against canned incidents.

### Model choice

**Primary: GPT-OSS 20B.** Uses ~13.7GB VRAM, generates at ~42 tokens/second, strong tool calling and structured output. The two capabilities that matter for this architecture — reliable function calling and clean JSON adherence to typed contracts — are exactly where GPT-OSS 20B is strongest in its weight class.

Fallbacks, in order of preference:

| Model | Footprint | Why you'd pick it |
|---|---|---|
| Qwen 3 14B (Reasoning) | ~9GB at Q4 | More context headroom; stronger reasoning chains; slightly weaker tool calling |
| Qwen 2.5 Coder 14B | ~9GB at Q4 | If the demo emphasises the Dev agent's PR authoring path |
| Mistral Nemo 12B | ~7GB at Q4 | Reliable middle ground; boring but works |
| Llama 3.1 8B Instruct | ~5GB at Q4 | Tiny, fast; weaker reasoning but responsive for live demos |

**Avoid for this demo:**

- Large-total MoE models (GLM-4.5-Air, Qwen3-30B-A3B) — active params are small but the full weights must load; doesn't fit at usable quantization in 16GB.
- DeepSeek R1 distills as the orchestrator — thinking tokens inflate context fast, which is exactly the sprawl problem the architecture is designed to avoid.

### Docker compose

```yaml
services:
  ollama:
    image: ollama/ollama:latest
    container_name: ollama
    ports:
      - "11434:11434"
    volumes:
      - ollama-data:/root/.ollama
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    restart: unless-stopped

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
      - OPENAI_BASE_URL=http://ollama:11434/v1
      - OPENAI_API_KEY=ollama
      - MODEL_NAME=gpt-oss:20b
      - POSTGRES_URL=postgresql://postgres:devops@postgres:5432/incidents
      - EVIDENCE_S3_ENDPOINT=http://minio:9000
      - EVIDENCE_S3_BUCKET=incidents
    depends_on:
      - ollama
      - postgres
      - minio

volumes:
  ollama-data:
  postgres-data:
  minio-data:
```

First-run:

```bash
docker compose up -d
docker exec ollama ollama pull gpt-oss:20b
```

On Apple Silicon, remove the `deploy.resources` block — Docker on Mac can't passthrough the Neural Engine. Better to run Ollama natively on the host and point the containerised agent at `http://host.docker.internal:11434`; that uses unified memory properly and is materially faster than CPU-bound Docker.

### Mixed deployment (recommended for anything beyond demo)

Local models handle tool-calling mechanics convincingly but show their seams on hypothesis synthesis under ambiguity and nuanced PR authoring. The practical pattern for production is **local collectors, cloud orchestrator**:

![mixed-deployment](diagrams/mixed-deployment.svg)

**Rationale:** collectors are high-volume and mechanical — they benefit from being local (no per-call cost, low latency, data never leaves the VPC). The orchestrator and dev agent are low-volume but high-stakes — they benefit from frontier-model reasoning. This split typically cuts cloud spend by 70%+ versus all-cloud while keeping the hard-reasoning quality where it matters.

Config surface for this pattern lives on `IncidentState.phase` — each phase has its own model binding, so routing is declarative rather than hardcoded into prompts.

### Demo caveats worth framing upfront

So the audience doesn't judge the architecture by the local model's ceiling:

- Local 20B handles the *mechanics* (tool calls fire, state updates happen, PRs draft) but not the *synthesis* (nuanced root-cause narratives, cross-service correlation under ambiguity).
- Prompt caching — the single biggest cost lever for cloud deployment — doesn't apply to local inference, so observed token economics don't extrapolate.
- Response times on 16GB will feel slower than production would. 42 t/s is fine for a chat demo; it's noticeable across a 20-step investigation loop.

---

## Related documents

- **`devops-agent-build-fleet.md`** — companion doc describing a fleet of specialist agents that constructs this runtime system. Mirrors the same patterns (narrow ephemeral specialists, stateful orchestrator, typed work-item contracts, verifier loop). Useful if you're building with Claude Code subagents, Archon Agent Work Orders, or a custom multi-agent build pipeline.
