# DevOps Support Agent — Architecture

An agentic system that receives incidents from Slack, diagnoses against cloud infrastructure and the LGTM observability stack, and either opens a PR to fix the issue, executes an operational remediation, or escalates to an on-call engineer with structured context.

Designed around two load-bearing constraints:

1. **Observability data is enormous.** Raw log/trace/metric payloads must never touch agent context. Collectors summarise and reference; raw data lives in an evidence store.
2. **Context sprawl kills agentic systems.** Memory lives in exactly one place (the orchestrator's `IncidentState`). Everything else is ephemeral and stateless.

---

## Design principles

- **One collector per data source, stateless.** Each invocation is `(question, scope) → (finding, evidence_id)`. No cross-invocation memory, no drift.
- **Evidence store as the shared substrate.** Raw payloads keyed by `evidence_id`. Agents pass references, never blobs.
- **Single source of truth for incident state.** The orchestrator holds `IncidentState`; every other node receives a slice.
- **Pre-flight remediation gate.** Not every investigation ends in a PR. A typed `RemediationPlan` decides: `pr`, `rollback`, `scale`, `flag_flip`, `runbook`, or `none`.
- **Separated lifecycle from investigation.** Intake, Investigator, and Coordinator are distinct prompts with distinct responsibilities. Conflating them causes prompt drift.
- **Structured escalation, not silent failure.** When the agent gives up, the human inherits hypotheses, evidence IDs, and attempt history.

---

## System architecture

```mermaid
flowchart LR
  subgraph INPUTS["Inputs"]
    direction TB
    slack["Slack message (support)"]
  end

  subgraph SOURCES["Data sources"]
    direction TB
    k8s["Kubernetes"]
    grafana["Grafana / LGTM"]
    other["Other sources"]
  end

  subgraph STORE["Shared state"]
    direction TB
    evidence["Evidence store (blobs by evidence_id)"]
    state["IncidentState (hypotheses, findings, timeline)"]
  end

  subgraph AGENTS["AI agents"]
    direction TB
    intake["Intake agent"]
    investigator["Investigator (orchestrator)"]
    collector["Collectors (ephemeral, per-source)"]
    validator["Hypothesis validator"]
    planner["Remediation planner"]
    dev["Dev agent (fix author)"]
    verifier["Fix verifier (dry-run / plan / diff)"]
    coordinator["Coordinator (lifecycle)"]
  end

  subgraph TRACKING["Tracking"]
    direction TB
    linear["Linear ticket"]
  end

  subgraph HUMAN["Human review"]
    direction TB
    reviewer["PR reviewer"]
    oncall["On-call engineer (handoff mode)"]
  end

  subgraph OUTPUTS["Outputs"]
    direction TB
    pr["Pull request"]
    ops["Operational action (rollback / scale / flag)"]
    escalation["Slack #devops (structured handoff)"]
  end

  slack --> intake
  intake -->|create| linear
  intake --> investigator

  investigator <-->|hypothesis-framed questions, N loops| collector
  collector --> k8s
  collector --> grafana
  collector --> other
  k8s --> collector
  grafana --> collector
  other --> collector
  collector -->|raw payload| evidence
  collector -->|summary + evidence_id| investigator

  investigator <-->|confirm / falsify| validator
  investigator <--> state
  validator -.->|reads| evidence

  investigator -->|root cause + evidence refs| planner

  planner -->|type = pr| dev
  planner -->|type = rollback/scale/flag/runbook| coordinator
  planner -->|type = none / insufficient| coordinator

  dev -.->|reads| evidence
  dev --> verifier
  verifier -->|pass| pr
  verifier -->|fail| dev

  pr --> reviewer
  reviewer -->|approved| coordinator
  reviewer -->|changes on fix| dev
  reviewer -->|challenges root cause| investigator

  coordinator -->|update status| linear
  coordinator -->|execute + log| ops

  investigator -.->|attempts exhausted| coordinator
  coordinator -->|structured summary| escalation
  escalation --> oncall
  oncall -.->|continues in ticket, agent available as tool| investigator

  linear -.->|iterate on feedback| investigator
```

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

```mermaid
flowchart TB
  subgraph WORKING["Working state (in-context)"]
    incident["IncidentState<br/>alert, hypotheses, findings,<br/>timeline, current focus"]
  end

  subgraph SEMANTIC["Semantic memory (retrieved per step)"]
    runbooks["Runbooks"]
    arch["Architecture docs"]
    catalog["Service catalog"]
    slos["SLOs"]
  end

  subgraph EPISODIC["Episodic memory (retrieved at intake)"]
    past["Past incidents<br/>symptoms -> root cause -> fix<br/>vector-indexed"]
  end

  subgraph ENTITY["Entity memory (cached)"]
    services["Per-service fact sheets<br/>owners, deps, recent deploys"]
  end

  subgraph EVIDENCE["Evidence store (by ID)"]
    blobs["Raw log / trace / metric / diff payloads<br/>S3 or Postgres, TTL-bounded"]
  end

  investigator[Investigator] --> incident
  investigator -.-> SEMANTIC
  investigator -.-> EPISODIC
  investigator -.-> ENTITY
  collector[Collector] -.->|writes| blobs
  collector -->|references| incident
  verifier[Verifier] -.->|reads| blobs
  dev[Dev agent] -.->|reads| blobs
```

**Key boundaries:**

- Working state is the only memory always in context.
- Semantic memory is retrieved per step against the current hypothesis, not pre-loaded.
- Episodic memory is queried once at intake: "have we seen this before?"
- Entity memory is cached per service; cheap to maintain, expensive to recompute per turn.
- Evidence store is durable but TTL-bounded (30 days is usually enough for an incident lifecycle plus postmortem).

---

## State schema

```mermaid
classDiagram
    direction LR

    class IncidentState {
        +str incident_id
        +Alert alert
        +Hypothesis[] hypotheses
        +Finding[] findings
        +TimelineEvent[] timeline
        +Action[] actions_taken
        +str[] services_touched
        +str current_focus_hypothesis_id
        +int investigation_attempts
        +IncidentPhase phase
        +datetime created_at
        +datetime updated_at
    }

    class Alert {
        +str source
        +str raw_message
        +str service
        +Severity severity
        +datetime fired_at
        +dict labels
    }

    class Hypothesis {
        +str id
        +str text
        +float score
        +HypothesisStatus status
        +str[] supporting_evidence_ids
        +str[] refuting_evidence_ids
        +datetime created_at
    }

    class Finding {
        +str id
        +str collector_name
        +str question
        +str summary
        +str evidence_id
        +float confidence
        +str[] suggested_followups
        +datetime created_at
    }

    class TimelineEvent {
        +datetime ts
        +str actor
        +str event_type
        +str ref_id
    }

    class Action {
        +str id
        +ActionType type
        +str description
        +ActionStatus status
        +str ref_id
        +datetime executed_at
    }

    class CollectorInput {
        +str question
        +str hypothesis_id
        +TimeRange time_range
        +str[] scope_services
        +int max_internal_iterations
    }

    class CollectorOutput {
        +Finding finding
        +EvidenceRef evidence
        +int tool_calls_used
        +int tokens_used
    }

    class EvidenceRef {
        +str evidence_id
        +str storage_uri
        +str content_type
        +int size_bytes
        +datetime expires_at
    }

    class RemediationPlan {
        +str id
        +RemediationType type
        +str rationale
        +str[] evidence_ids
        +str[] target_repos
        +str[] target_services
        +str rollback_plan
        +float confidence
        +bool requires_human_approval
    }

    class FixProposal {
        +str id
        +str plan_id
        +str branch_name
        +FileChange[] changes
        +str commit_message
        +str pr_body
    }

    class FileChange {
        +str repo
        +str path
        +str diff
    }

    class VerificationResult {
        +bool passed
        +str[] checks_run
        +str[] failures
        +str dry_run_output
    }

    class PullRequest {
        +str url
        +int number
        +str repo
        +str status
        +str[] reviewers
    }

    class EscalationPackage {
        +str incident_id
        +Hypothesis[] hypotheses_considered
        +Finding[] key_findings
        +RemediationPlan[] attempts
        +str[] attempt_failure_reasons
        +str current_working_theory
        +str suggested_next_steps
    }

    class HypothesisStatus {
        <<enum>>
        open
        confirmed
        refuted
        abandoned
    }

    class RemediationType {
        <<enum>>
        pr
        rollback
        scale
        flag_flip
        runbook
        none
    }

    class IncidentPhase {
        <<enum>>
        intake
        investigating
        planning
        fixing
        verifying
        reviewing
        escalated
        resolved
    }

    class ActionType {
        <<enum>>
        query
        fix_attempt
        rollback
        scale
        flag_flip
        notification
    }

    class ActionStatus {
        <<enum>>
        pending
        executing
        succeeded
        failed
    }

    class Severity {
        <<enum>>
        critical
        high
        medium
        low
    }

    IncidentState "1" *-- "1" Alert
    IncidentState "1" *-- "many" Hypothesis
    IncidentState "1" *-- "many" Finding
    IncidentState "1" *-- "many" TimelineEvent
    IncidentState "1" *-- "many" Action

    Finding "1" --> "1" EvidenceRef : evidence_id
    Hypothesis "1" --> "many" EvidenceRef : supporting or refuting

    CollectorInput ..> Hypothesis : references
    CollectorOutput *-- Finding
    CollectorOutput *-- EvidenceRef

    RemediationPlan "1" --> "many" Finding : cites
    FixProposal "1" --> "1" RemediationPlan : plan_id
    FixProposal "1" *-- "many" FileChange

    VerificationResult ..> FixProposal : verifies
    PullRequest ..> FixProposal : created_from

    EscalationPackage "1" *-- "many" Hypothesis
    EscalationPackage "1" *-- "many" Finding
    EscalationPackage "1" *-- "many" RemediationPlan
```

**Deliberate choices:**

- `IncidentState` is the only durable container. Collectors receive a `CollectorInput` slice, not the whole state.
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

```mermaid
flowchart LR
  investigator["Investigator:<br/>root cause + confidence"] --> planner["Remediation planner"]
  planner -->|type = pr| dev["Dev agent -> PR"]
  planner -->|type = rollback| roll["Coordinator -> kubectl / ArgoCD rollback"]
  planner -->|type = scale| scale["Coordinator -> HPA / replica adjust"]
  planner -->|type = flag_flip| flag["Coordinator -> LaunchDarkly / Unleash"]
  planner -->|type = runbook| run["Coordinator -> execute runbook step"]
  planner -->|type = none| handoff["Coordinator -> escalation"]
```

**Rules:**

- Only `type = pr` goes to the Dev agent.
- Operational actions (`rollback`, `scale`, `flag_flip`, `runbook`) go to the Coordinator and execute against cloud/k8s APIs with full audit logging in `IncidentState.actions_taken`.
- `requires_human_approval=True` on the plan forces a Slack confirmation before the Coordinator acts — default `True` for anything that mutates production.
- `type = none` is a clean escalation path when the agent has investigated but has no actionable remediation.

---

## Escalation path

Escalation is a **handoff**, not a failure. The on-call engineer receives a structured package and the agent stays available as a tool.

```mermaid
sequenceDiagram
    participant I as Investigator
    participant C as Coordinator
    participant S as Slack #devops
    participant O as On-call
    participant L as Linear

    I->>I: attempt N failed or confidence below threshold
    I->>C: EscalationPackage
    C->>L: update ticket status = escalated
    C->>S: post structured summary + evidence links
    S->>O: notification
    O->>I: ask follow-up question (agent as tool)
    I->>O: answer from IncidentState + evidence store
    Note over O,I: agent retains context; human leads
    O->>L: resolve or continue manually
```

**What the EscalationPackage contains** (not "I failed 3 times"):

- Hypotheses considered, with scores and current status.
- Key findings with evidence IDs (linkable to Grafana/Loki dashboards).
- Attempts made, each with its rejection reason.
- Current working theory and confidence.
- Suggested next steps the agent couldn't execute itself (missing permissions, genuine ambiguity, needs human judgement).

---

## Subagents and token management

Investigation phases that generate a lot of tokens internally but only need to return a conclusion run as **subagents with their own context windows**. The orchestrator sends a task, the subagent burns tokens locally, returns a structured finding.

```mermaid
flowchart TB
  orch["Orchestrator<br/>(lead investigator)"] 
  orch -->|dispatch| loki["Loki collector"]
  orch -->|dispatch| tempo["Tempo collector"]
  orch -->|dispatch| prom["Prometheus collector"]
  orch -->|dispatch| kube["Kubernetes collector"]
  orch -->|dispatch| gh["GitHub collector<br/>(recent deploys/PRs)"]
  orch -->|dispatch| hist["Historical incidents<br/>(episodic memory)"]

  loki -->|Finding| orch
  tempo -->|Finding| orch
  prom -->|Finding| orch
  kube -->|Finding| orch
  gh -->|Finding| orch
  hist -->|Finding| orch
```

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

```mermaid
flowchart LR
  subgraph LOCAL["Local (GPT-OSS 20B via Ollama)"]
    direction TB
    loki_c["Loki collector"]
    tempo_c["Tempo collector"]
    prom_c["Prom collector"]
    k8s_c["k8s collector"]
    gh_c["GitHub collector"]
  end

  subgraph CLOUD["Cloud (Claude Sonnet)"]
    direction TB
    intake_c["Intake"]
    invest_c["Investigator"]
    plan_c["Planner"]
    dev_c["Dev agent"]
    verify_c["Verifier"]
    coord_c["Coordinator"]
  end

  intake_c --> invest_c
  invest_c <-->|cheap, high-volume queries| LOCAL
  invest_c --> plan_c
  plan_c --> dev_c
  dev_c --> verify_c
  verify_c --> coord_c
```

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
