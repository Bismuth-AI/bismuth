# 0018 — Bound active context, not useful tool calls

**Status:** accepted
**Amends:** [0015](0015-agentic-shadow-planning.md),
[0017](0017-incremental-arrival-windows.md)

## Context

The first incremental planner limited all navigation tools to five successful calls and
the verifier to four. In a real run, `tree` and four folder notes consumed the main
allowance before the planner could inspect affected inventories. The disabled tools stayed
visible, so the model spent sixteen later calls receiving the same budget error and reached
the 24-turn guard before `submit_plan`.

The limit bounded a proxy rather than the scarce resource. A one-line note and a large
inventory page cost the same call, while rejected calls still consumed model turns. Folder
notes at the time were also deterministic `axis: class` strings, so the agent had little
durable boundary memory and needed more local evidence than the allowance permitted.

Long-running coding agents instead retain a raw history while controlling the projection
that remains in the model context. Old or unusually large tool observations are offloaded,
recent observations remain inline, and the agent can retrieve an exact archived result by
a short address. A high turn guard remains a circuit breaker rather than being the normal
way a successful run ends. ADR-0026 replaces the same-transcript protected conclusion phase
with a fresh conclusion request.

## Decision

Agent Kit owns provider-neutral active-context management:

- the host still receives the complete raw transcript in `RunResult`;
- active-context pressure is estimated conservatively without a provider tokenizer;
- large and old tool observations move to a per-run addressable archive;
- placeholders expose short `R000001` identifiers and `recall_tool_result` pages exact
  archived content back into the active context;
- old tool-call arguments are cleared once their result is addressable;
- an identical tool name and argument object may run twice, but a third request closes
  exploration; a further identical request stops the stalled loop;
- tool schemas remain stable within one transcript; a fresh conclusion transcript exposes
  only host-declared terminal tools and carries flattened observations, not historical tool calls.

Bismuth no longer assigns a shared call count to `tree`, `inventory`, `read`, `grep`,
`read_note`, or `ls`. The main librarian has a larger circuit-breaker allowance and a
fresh forced-tool conclusion request. Its verifier uses the same explore/conclude split in
independent contexts. Agent turns, tool calls/results, compaction events, unavailable tools,
and stop reasons are persisted to the ordinary trace log with planner/verifier stage labels.
Each arrival window also schedules one affected non-root boundary in its own fresh context.
The root planner remains responsible for the common boundary, while the scoped pass lets
large top-level shelves grow deeper without loading every sibling subtree into one agent.

The two-new-sibling invariant applies when a parent has no established boundary. Once
direct siblings already exist, one additional sibling is a new value of that boundary,
not a one-class partition. Existing direct children therefore count when validating the
resulting sibling set.

A move names its direct child class relative to the boundary parent. The validator joins
the two deterministically while retaining the legacy full-path form for compatibility.
New nested shelves therefore do not depend on the model repeating a path prefix correctly.

Within one maintenance drain, affected scopes are reviewed breadth-first and at most once
before the remaining windows complete. A repeatedly large folder can no longer starve its
siblings merely because every arrival window happens to contain one of its documents.

Folder notes also persist the parent boundary basis, question, and this child's answer.
Their human-readable purpose is deterministically rendered from that state. Character
count is not a semantic acceptance criterion; generation transport guards remain separate.
These fields change only with the structural boundary, not with inventory churn.

## Consequences

- Cheap reads no longer prevent later decisive evidence from being inspected.
- Repetition is stopped by lack of progress, not by charging unrelated reads from one pool.
- Context use is bounded independently of the number of tool calls, while exact old
  observations remain recoverable during the run.
- Raw trace observability and the atomic shadow-plan transaction are unchanged.
- Compaction can still hide a fact from immediate attention. The short archive address and
  pinned recent observations make that loss recoverable, but model quality must be checked
  in real vault runs.
- The turn ceiling is still finite. Reaching it is an abnormal safety stop and remains a
  retryable maintenance failure rather than a successful no-op.

## Revisit when

- provider token accounting can replace the conservative local estimate;
- real traces show the model recalls archived evidence too often or too rarely; or
- one affected subtree routinely needs more evidence than a single managed context, in
  which case add scope-isolated investigator agents without changing the shadow-plan or
  transaction boundary.
