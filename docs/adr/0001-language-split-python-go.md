# ADR-0001: Language split — Python orchestrator, Go collectors

- Status: accepted
- Date: 2026-04-21

## Context

The runtime agent has two populations of code with very different profiles:

1. **Reasoning layer** — Intake, Investigator, Planner, Dev, Verifier, Coordinator. LLM-heavy, Pydantic-typed, interacts with the Anthropic SDK and a graph framework. Low throughput, high stakes per call.
2. **Collector layer** — Prometheus, Loki, Kubernetes. HTTP-in, HTTP-out, writes raw blobs to the evidence store, returns a summarised `Finding`. High throughput, mechanical, heavy use of official protocol clients (`client-go`, Prometheus API client).

Python is the natural home for the reasoning layer: Pydantic v2 for typed state, LangGraph/PydanticAI for orchestration, first-class Anthropic SDK, mature async HTTP. Go is the natural home for the collector layer: `k8s.io/client-go` is the canonical k8s client and is Go-only; the Prometheus client libraries are Go-native; goroutines fit the parallel-collector-dispatch pattern; static binaries make local-deployment collectors (per post-MVP mixed deployment) trivial.

A single-language shop would work but forces us to either (a) consume k8s via `kubernetes` Python client (serviceable but second-class) or (b) re-implement the reasoning layer in Go without ecosystem support.

## Decision

- **Python** owns: `agent/` — Pydantic schemas, LangGraph orchestrator, agent roles, Intake webhook, evidence-store client, prompts, tests.
- **Go** owns: `collectors/` — one binary per data source under `cmd/`, shared contract and evidence code under `internal/`. Custom MCP servers (if any) also go here.
- **Seam**: plain HTTP. Each collector exposes `POST /collect` taking a `CollectorInput` JSON body and returning a `CollectorOutput`. We'll wrap with MCP later if the benefit materialises, but HTTP first keeps the contract trivially testable.
- **Contract duplication**: the `CollectorInput`/`CollectorOutput` shapes exist in both Pydantic (`agent/src/agent/schemas/collector.py`) and Go (`collectors/internal/contract/contract.go`). A round-trip test on a shared JSON fixture catches drift.

## Consequences

- **+** Each layer uses the ecosystem it's best suited for. No "Python by force" in collectors, no "Go by force" in the orchestrator.
- **+** Collectors can be deployed locally (per the mixed-deployment post-MVP plan) without the orchestrator having to change.
- **−** Two toolchains. Onboarding is slightly heavier; we need `uv` AND `go`. Offset by narrow ADRs for each (0002, 0003).
- **−** Contract duplication. Mitigated by a JSON round-trip test and by treating the Python schema as the source of truth — the Go struct must match it, enforced in the collector-specialist agent's acceptance criteria.
- The `agent-builder` and `collector-specialist` subagents target different directories and tool surfaces; the build fleet's path-scoping rules already prevent cross-contamination.
