# Launching the three tracks

This is the prompt template for kicking off Alpha, Beta, and Gamma in parallel.
Run all three subagent invocations in a single Claude Code message so they
execute concurrently. They will naturally serialise where the gate file forces
it — that is the point of the gate.

## Common preamble (paste at the top of every track prompt)

> You own the **<TRACK>** stream defined in `backlog/streams/<track>/`.
> Open `backlog/streams/dependencies.md` first. For each task in your stream,
> in declared order:
>
> 1. Read the task's spec file.
> 2. Confirm every `depends_on` ID is `done` in `dependencies.md`. If not,
>    stop and report "blocked on `<ID>`" — do not start.
> 3. Update the task's row to `in_progress`, set `Owner` to your agent name,
>    and commit that change with the message
>    `chore(streams): claim <ID>`.
> 4. Implement the deliverables. Lint + tests must pass.
> 5. Flip the row to `done` and commit with `feat(streams): complete <ID>`.
>
> Never mark a row `done` if tests are red. Never expand scope silently — open
> a new row in `dependencies.md` and a new file under
> `backlog/streams/<track>/` for any newly-discovered work.

## Track Alpha — shared plumbing

Subagent: `agent-builder` (with `evidence-specialist` for F-06).

Order: F-01 → F-02 → F-03 → F-04 → F-05 → F-06 → F-07.
F-05 is independent of the LLM stack — fine to interleave with F-01.
F-06 has no upstream blockers and is the slowest; start it early.

## Track Beta — reasoning agents

Subagent: `agent-builder`.

Order:
1. **B-01** Investigator (waits on F-01..F-04)
2. **B-02** Collectors dispatch (waits on F-04, F-06)
3. **B-03** Planner (waits on F-01..F-03, F-05)
4. **B-04** Dev
5. **B-05** Verifier
6. **B-06** Reviewer
7. **B-07** Coordinator (waits on F-05, F-06, B-03)

Beta will be blocked at the gate until Alpha lands the foundation pieces —
expected idle time at start of run; that is normal.

## Track Gamma — Go collectors

Subagent: `collector-specialist`.

Order: C-01 → (C-02 ‖ C-03 ‖ C-04 in parallel).
Gamma is fully independent of Alpha and Beta after C-01 lands.

## Cross-track integration

After all three tracks are `done`, hand off to the **Reviewer** subagent for
**X-01** — the end-to-end happy-path run against the lab cluster. Acceptance:
firing the OOM alert through `task monitoring:alerts` produces an
`IncidentState` that traverses Intake → Investigator → Collectors → Planner →
Coordinator with `phase=escalated` and a non-empty `EscalationPackage`.
