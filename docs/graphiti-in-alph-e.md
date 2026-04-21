# Graphiti in alph-e — Integration Proposal

**Status:** Draft for review
**Companion to:** `docs/devops-agent-architecture.md`, `docs/devops-agent-build-fleet.md`
**Target:** alph-e runtime (Investigator / Collectors / Coordinator graph)

---

## TL;DR

Graphiti replaces the `pgvector` line in the current stack for the **semantic**, **episodic**, and **entity** memory layers — the three layers of alph-e's memory architecture that are *not* `IncidentState` working memory. It does not replace Postgres (still the source of truth for `IncidentState`, `EvidenceRef` metadata, action logs) and it does not replace MinIO (still the raw-payload blob store).

The win is specific and load-bearing for alph-e: bi-temporal edges let the Investigator query "what did we believe about service X at the time of incident Y?", entity extraction turns collector findings into a reusable service graph instead of a pile of vectors, and the existing Graphiti MCP server plugs into the collector tool surface with zero new client code.

---

## 1. Why this, and why now

alph-e's architecture doc already names the memory problem precisely. Four layers with different lifetimes and retrieval patterns, of which working state is the only thing the Investigator keeps in context. The other three — semantic, episodic, entity — need an external substrate that supports *refined retrieval*: the Investigator pulls small, high-signal slices against a hypothesis, not documents.

The suggested stack says Postgres + pgvector for this, with the caveat "simple, adequate; don't over-engineer." That's a fine default, and for semantic memory alone (runbooks, postmortems as chunks) it's enough. It stops being enough the moment you ask the two questions alph-e will actually ask:

*"Have we seen an incident like this before, and what did we know about the affected service at that time?"* — this is episodic memory with temporal validity. pgvector can approximate it with a `timestamp` column and a filter, but it can't express "service X was believed to own feature Y between 2026-01-12 and 2026-03-04" natively.

*"What services depend on db-primary, and which of them were deployed in the last hour?"* — this is an entity-memory query with graph shape. pgvector can't do it at all; you'd hand-roll a separate service graph table.

Graphiti is purpose-built for both. It gives you a single store that serves semantic (embeddings + BM25), episodic (bi-temporal edges with `valid_at` / `invalid_at`), and entity (graph nodes with typed relationships, extracted from text automatically via the LLM on ingest). The cost is a new backend (Neo4j, FalkorDB, Kuzu, or Neptune) and LLM spend on the ingest path.

## 2. What Graphiti is, briefly

Python library (`graphiti-core`) plus an optional MCP server (`mcp_server/` in the repo). You hand it episodes — text, JSON, or message transcripts — and it extracts entities and relationships, embeds them, writes them to a graph backend. Every edge carries four timestamps: `created_at`, `valid_at`, `invalid_at`, `expired_at`. When a new episode contradicts an old fact, the old edge gets `invalid_at` set, not deleted — you can still ask what was true at a prior point in time.

Search is hybrid: BM25 + vector similarity + graph traversal, fused with RRF. You can search for nodes, edges, or full episodes. Multi-tenancy is via `group_id` on every operation; alph-e will use this per-cluster (or per-team, if you go multi-tenant later).

The trade-offs worth naming up front: entity extraction hits an LLM on every `add_episode`, so high-volume ingestion has a real cost line. Write latency is seconds, not milliseconds. You now run a graph DB alongside Postgres and MinIO — one more thing to back up, upgrade, and monitor. None of these are dealbreakers for alph-e's volume (incidents, not events; tens per day, not millions), but they do shape *what* you feed into Graphiti and what you keep in Postgres.

## 3. Where Graphiti attaches in alph-e

### 3.1 Replaces pgvector for semantic memory

**File surfaces this touches (expected when the runtime lands):** whatever module ends up owning "retrieve relevant runbooks / postmortems / prior decisions for this hypothesis" — likely something like `agents/investigator/context.py` or a shared `memory/semantic.py`.

Semantic memory is the simplest migration. Today: chunk runbooks and postmortems, embed with pgvector, retrieve top-k on hypothesis keywords. With Graphiti: `add_episode(episode_body=runbook_markdown, source=EpisodeType.text, reference_time=runbook.authored_at, group_id="cluster-prod")` on ingest; at retrieval the Investigator calls `client.search(query=hypothesis.text, group_ids=["cluster-prod"], num_results=8)`. The return shape is richer — you get back edges with provenance and temporal validity — which also happens to be what a hypothesis scorer wants.

This one change is enough to justify the integration on its own if you ever decided not to do the other two.

### 3.2 Owns the episodic layer outright

This is where Graphiti pays for itself.

**Intake (`agents/intake/`):** when an incident arrives from Slack, the architecture says "queried once at intake: have we seen this before?". That query becomes:

```python
prior = await client.search(
    query=alert.signature,              # "OOMKilled on leaky-service in demo namespace"
    group_ids=[cluster_id],
    num_results=5,
    filters=SearchFilters(
        node_labels=["Incident", "RootCause"],
        valid_at=alert.fired_at,        # point-in-time filter
    ),
)
```

The `valid_at` filter is the part that plain pgvector can't do honestly. You want matches against what was *believed at the time of similar past incidents*, not against today's entity graph that has since been edited by five later investigations.

**Incident close (`agents/coordinator/` on resolve):** the Coordinator writes one episode per resolved incident, containing the refined narrative — hypotheses considered, the winning one, the remediation type, evidence IDs, outcome. This is the corpus Intake will query next time.

```python
await client.add_episode(
    name=f"incident_{incident.id}",
    episode_body=json.dumps(incident.episodic_summary()),
    source=EpisodeType.json,
    source_description="resolved incident",
    reference_time=incident.resolved_at,
    group_id=incident.cluster_id,
)
```

Do *not* write every collector invocation as an episode. That's where LLM spend runs away. Episodes are incident-level artifacts. Per-collector learning, if you want it, goes into the entity layer below.

### 3.3 Becomes the entity memory layer

The architecture doc says entity memory is "cached per service; cheap to maintain, expensive to recompute per turn." That's exactly a knowledge graph.

Populate the graph from two sources. First, static service catalog at build time — one episode per service with owner, dependencies, SLOs, on-call rotation, deploy pipeline. Second, collector findings during incidents — when the Loki collector returns `"847 'connection refused' errors from api-gateway → db-primary onset 14:02:17"`, that's a refined finding the Coordinator can fold into an episode at incident close:

```python
await client.add_episode(
    name=f"incident_{incident.id}_facts",
    episode_body=json.dumps({
        "affected_services": ["db-primary", "api-gateway"],
        "dependency_chain": ["api-gateway -> db-primary"],
        "failure_mode": "connection_refused_saturation",
        "validated_at": incident.resolved_at.isoformat(),
    }),
    source=EpisodeType.json,
    group_id=incident.cluster_id,
)
```

Graphiti extracts nodes (`db-primary`, `api-gateway`), edges (`depends_on`, `affected_by`), and stores them with the right temporal metadata. The *next* Investigator, hypothesising about api-gateway, can call `client.search_nodes(query="api-gateway dependencies")` and get the live dependency subgraph, scoped to what was true at a given time if needed.

This is the layer where Graphiti's graph-native features (not just vector search) actually matter. A pgvector-only approach forces you to hand-roll a service graph table anyway — it's just cheaper to let Graphiti do it.

### 3.4 Evidence store and IncidentState stay put

Explicitly out of scope for Graphiti:

Raw observability payloads (log bundles, Prometheus range queries, trace JSON) stay in MinIO, referenced by `EvidenceRef.storage_uri`. They are too big for the graph and don't benefit from being there.

`IncidentState` — working memory, owned by the Investigator — stays in Postgres. It is transactional, hot-path, and single-writer (the orchestrator). Putting it in a graph would be architecture fanfic.

`EvidenceRef` metadata — `evidence_id`, `storage_uri`, `size_bytes`, `expires_at` — stays in Postgres. It's referenced *by* graph entries (as an `evidence_id` attribute on an episode or edge), but lives in the relational store where TTL lifecycle rules already work.

The resulting division of labour is clean: **Postgres is source-of-truth for the live incident; Graphiti is the learned substrate across incidents; MinIO is the raw-byte archive.**

## 4. Retrieval strategy

Graphiti gives the Investigator three query shapes it doesn't have today. They map to three moments in the loop.

At intake, `search(query, valid_at=alert.fired_at)` answers "have we seen this before?" — returns incident episodes ranked by hybrid score, temporally valid.

During hypothesis scoring, `search_nodes(query)` plus one or two `search_facts` calls answer "what does the graph say about these entities?" — returns the relevant subgraph for a hypothesis, which the Investigator folds into its working state as refined facts, not blobs.

At remediation planning, `search_facts(query, edge_types=["caused_by", "mitigated_by"])` answers "what worked last time?" — returns edges linking failure modes to remediations, which the Planner uses as a prior for the `RemediationPlan.type` decision.

All three calls are small and structured. None of them return prose the agent has to scan.

## 5. Fit with the existing MCP + collector pattern

alph-e's design leans heavily on MCP servers as the composable integration surface ("Cloud / k8s tools: MCP servers per provider", "Observability tools: MCP servers for Grafana / Loki / Tempo / Prometheus"). Graphiti ships one.

The ergonomic move is to run `mcp_server/` from the Graphiti repo as another MCP server in the investigator's tool surface, alongside the Loki/Prom/Tempo ones. Tool names surfaced to the agent: `add_episode`, `search_nodes`, `search_facts`, `get_episodes`, `delete_episode`. The Investigator treats the memory graph as one more read-path collector — `search_nodes` is shaped identically to any other collector's "ask a scoped question, get back a refined finding."

Write-path episodes (`add_episode` at intake / close) can go through the MCP server too, or through the Python SDK directly if you prefer to keep the Coordinator's write discipline typed at compile time. Both work; the MCP route is simpler, the SDK route is better if you want to enforce episode-shape invariants in Pydantic models before they reach Graphiti.

## 6. Storage backend choice

**Recommendation: FalkorDB for the local demo, Neo4j for cloud.**

The local demo runs on a 16GB box with Ollama, Postgres, MinIO, and the agent already competing for memory. FalkorDB is Redis-based, sub-10ms reads, and has a tiny footprint — it's the right call when the existing docker-compose is already tight. Graphiti supports it as a first-class backend.

For cloud, Neo4j is the default Graphiti backend, has the best ecosystem, and handles the graph sizes alph-e will produce without thinking about it. Hosted Aura or a small self-managed instance — either works.

Kuzu is an option if you want zero new services (it's embedded, like SQLite) but the ergonomics of running it alongside Postgres in alph-e's deployment model are not worth the savings; you still need something persistent across restarts.

Amazon Neptune is overkill and adds AWS lock-in that alph-e has so far avoided (MinIO instead of S3, self-hosted Loki). Skip.

## 7. Cost control and ingest discipline

The LLM-on-ingest cost is the biggest operational risk. alph-e is low-volume at the incident level but high-volume at the collector level — if every collector finding becomes an episode, spend will surprise.

Three rules keep this bounded:

**Episodes are incident-level.** One at intake (the alert payload, for future "have we seen this" queries), one at close (the refined narrative + extracted facts). Collectors do not write episodes.

**Use `add_episode_bulk` and async dispatch.** At incident close the Coordinator batches the write rather than blocking the resolution path. The existing LangGraph/PydanticAI flow has an obvious seam for this — the post-resolve tick.

**Cheap model for extraction.** Graphiti lets you configure the extraction LLM independently. Haiku is plenty for entity/relation extraction from structured JSON episodes; reserve Sonnet for the Investigator's own reasoning. This typically cuts the Graphiti ingest line by ~80% versus defaulting to the orchestrator's model.

With these three rules, expected ingest cost at ~50 incidents/day is in the low tens of dollars per month, comfortably below the current cloud LLM budget.

## 8. Migration from pgvector

There isn't much to migrate. The architecture doc is still a design, not a deployed system — so this is "choose Graphiti instead of pgvector for three of the four memory layers" rather than "port data." Concretely:

Replace the `pgvector/pgvector:pg16` line in the demo docker-compose with `falkordb/falkordb:latest` (or `neo4j:5.26-community` if you want parity with cloud).

Drop the `pgvector` extension from the Postgres schema. Keep Postgres as-is for `IncidentState`, `EvidenceRef`, `actions_taken`, the action audit log.

Add `graphiti-core` to the agent's Python deps and configure it against the chosen backend. The `agents/investigator/context.py` / `agents/intake/` / `agents/coordinator/` surfaces wire up `client.search()` and `client.add_episode()` at the three moments in section 3.

Seed the episodic layer from existing postmortems (the architecture doc already calls this out as a one-time ETL in Open Decisions #5 — "Episodic memory bootstrap"). Every postmortem markdown becomes one `EpisodeType.text` episode with `reference_time = incident_occurred_at` and `group_id = cluster_id`.

## 9. Open questions

**Is `group_id` per cluster, per team, or both?** Start per cluster (`cluster_id`). Add a second dimension if/when alph-e goes multi-tenant. Graphiti supports crossing multiple `group_ids` in a single query, so this is forward-compatible.

**Do collector findings write to Graphiti directly, or only through the Coordinator at incident close?** Recommendation: only through the Coordinator, even though the plumbing would allow direct writes. This preserves the architectural property that collectors are stateless and write nothing durable. Deferred learning at close is fine; the benefit of "slightly more current graph" doesn't justify eroding the contract.

**Do subagents see Graphiti directly, or only the Investigator?** Recommendation: only the Investigator (and the Intake agent at its single query point). Subagents already run with bounded context; adding a graph query surface to them re-introduces the sprawl the architecture is designed to prevent. If a subagent needs a graph fact, it comes in via the `CollectorInput` the Investigator assembled.

**When do entity nodes get retired?** Services get decommissioned, owners change, dependencies move. Graphiti's `invalid_at` handles fact-level supersession automatically on re-ingest of contradicting episodes, but a stale *node* with no invalidating episode can linger. Introduce a weekly reconciler job that diffs the service catalog source of truth (whatever that ends up being — IaC repo, Backstage, a YAML file) against Graphiti's service nodes and writes corrective episodes.

**Do we need pgvector at all after this?** Probably not. If the only pgvector usage was the three memory layers, it comes out of the stack entirely. Revisit only if a future feature genuinely needs embeddings on tables that live in Postgres (e.g. similarity search over `IncidentState` by summary) — which would be a different feature, not a migration.

---

## Appendix A — Concrete stack diff

Existing (from `devops-agent-architecture.md`, "Suggested stack"):

| Layer | Choice |
|---|---|
| Semantic + episodic memory | Postgres + pgvector |
| Evidence store | S3 / MinIO (blobs) + Postgres (metadata) |

Proposed:

| Layer | Choice |
|---|---|
| Working state (`IncidentState`) | Postgres |
| Semantic + episodic + entity memory | **Graphiti** on **FalkorDB** (local) / **Neo4j** (cloud) |
| Evidence store | S3 / MinIO (blobs) + Postgres (metadata) — unchanged |
| Graphiti extraction LLM | Haiku (separate from orchestrator model binding) |
| Memory tool surface to agents | Graphiti MCP server (`mcp_server/` from getzep/graphiti), added to the collector MCP fleet |

## Appendix B — Docker compose delta

```yaml
services:
  # replace pgvector/pgvector:pg16 with:
  postgres:
    image: postgres:16              # no pgvector extension needed
    environment:
      POSTGRES_PASSWORD: devops
      POSTGRES_DB: incidents
    # ...

  # add:
  falkordb:
    image: falkordb/falkordb:latest
    container_name: falkordb
    ports:
      - "6379:6379"                 # redis protocol
      - "3001:3000"                 # browser UI (optional)
    volumes:
      - falkordb-data:/var/lib/falkordb/data

  # agent gets new env vars:
  devops-agent:
    environment:
      # existing...
      - GRAPHITI_BACKEND=falkordb
      - GRAPHITI_URI=redis://falkordb:6379
      - GRAPHITI_EXTRACTION_MODEL=claude-haiku-4-5-20251001
      - GRAPHITI_GROUP_ID_DEFAULT=cluster-local

volumes:
  # existing...
  falkordb-data:
```

## Appendix C — Acceptance criteria for the integration work item

If this lands as a `WorkItem` in the build fleet (section "Initial backlog seed" of `devops-agent-build-fleet.md`), the acceptance criteria worth setting:

- `graphiti-core` available to the agent runtime; FalkorDB / Neo4j reachable from docker-compose.
- Intake calls `search` with `valid_at=alert.fired_at` and receives episodes for a seeded postmortem corpus.
- Coordinator writes one episode at incident close via `add_episode`; episode is retrievable via `search` in a later run.
- Service-catalog seeding script ingests a sample service inventory; `search_nodes("db-primary")` returns the node and its dependencies.
- Ingest LLM is configured independently of the orchestrator model — verified by a per-call token metric.
- No collector writes episodes directly; this is enforced by the collector base class not holding a Graphiti client.
- Existing evaluation corpus passes end-to-end: alert in → incident resolved with PR or operational action → episode written → re-running same alert now surfaces the prior episode at intake.

---

## Related

- `docs/devops-agent-architecture.md` — four-layer memory model, the reason this integration exists.
- `docs/devops-agent-build-fleet.md` — where this proposal is implemented (WorkItem under "memory" or "evidence" specialist depending on how you split scope).
- getzep/graphiti — library and MCP server.
- Zep's Graphiti docs — `add_episode`, `group_id` multi-tenancy, bi-temporal model.
