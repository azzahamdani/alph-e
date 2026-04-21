# ADR-0003: Go toolchain — Go modules + golangci-lint + testify

- Status: accepted
- Date: 2026-04-21

## Context

The `collectors/` codebase is a handful of small HTTP services built around official Go clients (`k8s.io/client-go`, Prometheus API client, Loki/Grafana clients). It needs:

- Standard module management — no vendor directory, no alternative build systems.
- Aggressive linting so collectors behave predictably under partial failures (network timeouts, empty responses, slice/map access patterns).
- Test ergonomics that work for table-driven tests (the default Go pattern for collector input/output validation).

## Decision

| Tool | Role |
|---|---|
| **Go modules** (`go.mod` / `go.sum`) | Dependency management |
| **golangci-lint** | Lint aggregator — bundles `vet`, `staticcheck`, `errcheck`, `gosec`, `revive`, `gocritic` |
| **stdlib `testing`** | Test runner |
| **`github.com/stretchr/testify`** | Assertions + mocks where readability wins over stdlib verbosity |

Go version floor: **1.23**. Needed for `slices`/`maps` stdlib packages and modern `k8s.io/client-go` releases.

`.golangci.yml` enables a conservative lint set at first: `errcheck`, `govet`, `staticcheck`, `gosimple`, `ineffassign`, `unused`, `gosec`, `revive`. Expand post-MVP1 once there's signal on which warnings matter.

## Consequences

- **+** Standard Go setup — any Go engineer lands immediately, no project-specific tooling to learn.
- **+** `golangci-lint` catches the usual footguns (unchecked errors, shadow variables, unused returns) that would otherwise show up as silent collector failures.
- **+** `testify` lets us write `require.NoError(t, err)` and `assert.Equal(t, expected, got)` instead of verbose stdlib assertions — collector tests are assertion-heavy.
- **−** Lint can feel chatty. Baseline is conservative; we'll tune `.golangci.yml` as patterns emerge.
- **−** `testify` is a dependency some Go purists avoid. Trade-off: readability in tests is worth more to us than zero-dep purity in a non-hot-path.
