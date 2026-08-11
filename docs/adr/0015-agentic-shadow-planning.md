# 0015 — Plan the library agentically, validate and apply it automatically

**Status:** accepted
**Supersedes:** the semantic maintenance pipeline in
[0014](0014-boundary-state-and-closed-membership.md)
**Retains:** 0014's transport guards, stable signs, and deterministic execution rules

## Context

The closed-membership pipeline was mechanically safe but failed its real purpose. In
a 300-document run it proposed the comparison label `범용 기본법 체계 vs 분야별
특별법`, then proposed labels made by concatenating paths and filenames. Schema length
limits cut those strings, path sanitisation converted the cut output into legal folder
segments, and model-authored audits approved the result. The transaction behaved
correctly while the library became worse.

This was not a missing blacklist. The deterministic pipeline had split one semantic
decision into local generation, per-document choices, reduction, and same-model audit.
Each part could satisfy its narrow contract while no component remained responsible
for the coherence of the complete library. Fake-model tests proved those contracts and
therefore did not detect the quality regression.

Running that pipeline after every arrival also made order effects and taxonomy churn
part of ordinary ingest. A librarian should observe a completed arrival set before
deciding whether the building itself needs to change.

## Decision

Semantic structure planning is a tool-using Agent Kit task. After an upload or scan
set is safely filed, one agent navigates the tree, paginated compact document cards,
folder notes, and selected sidecars. It maintains one whole-library model, asks a
context-isolated verifier to challenge the complete intended boundary, and submits
one shadow plan. Inventory assigns short deterministic `D000001` handles; membership
output contains those handles rather than long filenames or random identifiers, and
the application alone maps them back to paths.

The shadow plan is not HITL. It is an internal, non-mutating representation. Ordinary
code validates it twice: once when submitted and again immediately before execution.
Validation rejects missing or duplicated documents, paths outside the requested
scope, non-direct sibling targets, one-class boundaries, singleton new shelves,
collisions, filename shelves, and every name that filesystem sanitisation would alter.
Rejected plans leave the current tree unchanged and may be revised by the agent.

An accepted plan is compiled into one journal entry containing folder creation,
stable boundary notes, document moves, and sidecar moves. The transactor applies the
entry atomically and rolls it back on failure. No user approval is required. The
existing manual structure-review UI remains an optional correction surface, not the
autonomous librarian's execution path.

The previous `LibraryMaintenanceService` remains temporarily available as a diagnostic
comparison point but is no longer connected to per-document ingest. It must not mutate
the live tree automatically.

Agent calls stream every provider chunk into the raw LLM log and use both inactivity
and absolute timeouts. Their generous generation ceiling is a transport circuit
breaker, not a semantic folder-name or plan-quality rule.

## Consequences

- One component is accountable for the coherence of the entire proposed change.
- Ingest safety no longer depends on maintenance success.
- Taxonomy changes happen once per completed arrival set rather than once per file.
- Bad model text is rejected, never repaired into a different filesystem name.
- Folder notes are generated from accepted boundary state, not unconstrained prose.
- The agent can deliberately leave uncertain documents where they are.
- A large archive still requires bounded navigation and eventually hierarchical
  subplans; the present paginated inventory removes single-prompt expansion but does
  not claim unlimited context.
- Classification quality must be assessed on real runs. Static and fake-model tests
  can establish safety and wiring only.

## Revisit when

- real runs show that autonomous shadow plans regress established structures;
- archives routinely exceed one agent run's navigation context; or
- a provider cannot reliably produce native tool calls.

In those cases, preserve the same shadow-plan and transaction boundary while changing
how planning context is partitioned. Do not return to per-field character limits as a
semantic taxonomy design.
