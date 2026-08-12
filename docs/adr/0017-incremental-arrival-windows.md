# 0017 — Maintain between bounded arrival windows

**Status:** accepted
**Amends:** [0015](0015-agentic-shadow-planning.md), [0016](0016-resumable-maintenance.md)

## Context

Deferring maintenance until all 300 files in an upload were catalogued kept ingest safe,
but left every document in a young vault without a useful prior tree. The final planner
then tried to read the whole collection into one accumulating Agent Kit conversation.
Its verifier was context-isolated; the primary planner was not. Raising inventory to 500
cards reduced tool turns but merely concentrated the same unbounded context and would fail
again for 3,000 or 30,000 files.

Running 50 unrelated taxonomy planners is not the answer either. Each window inventing an
independent axis would make arrival order part of the folder language. The existing tree
and stable folder notes must carry decisions forward.

## Decision

Newly filed document IDs enter a durable arrival queue. Maintenance consumes a prefix of
at most 50 cards and at most 18,000 card characters. These are execution ceilings, not a
rule that the collection should be divided. The agent sees:

- the complete bounded `arrivals` window;
- the current tree and stable folder boundary notes produced by earlier windows; and
- bounded inventory pages only for folders affected by the new arrivals.

The arrival tool can return its window only once. The primary planner has five additional
read-tool calls and the isolated verifier has four; later calls return a short budget
message. Context therefore stays bounded even when a model ignores the prompt and keeps
trying to enumerate the vault.

After a window's shadow plan validates and applies, the next documents are placed against
that updated tree. Thus a 300-document upload normally performs six bounded maintenance
passes, and document 51 sees the structure learned from documents 1–50.

Small multi-file arrival sets flush their final partial window. A stable library receiving
single files does not run structural maintenance for every file: those IDs remain in the
durable waiting queue until a context window fills or the user explicitly requests
structure maintenance. Four documents may flush a new empty library because the existing
validator's two non-singleton siblings are mathematically impossible below four; this is
an execution feasibility check, not a semantic classification threshold.

If one window fails, file ingest continues and later IDs join the same durable backlog.
Automatic maintenance does not repeatedly hit the same broken model during that batch.
After configuration is fixed, retry consumes the backlog in the same bounded order and
updates the tree between windows. Source documents are never reparsed.

The model receives deterministic `D000001` handles for the current filesystem snapshot.
Persistent hash IDs stay inside application state and are never copied into model-authored
membership output.

## Consequences

- Planner context is bounded independently of upload size.
- Early structure becomes useful to later documents in the same upload.
- Existing boundary notes, not a monolithic conversation, are the memory between windows.
- A failed window does not cause five more expensive attempts against the same bad model.
- Results can still depend on evidence order, so real-vault order-independence measurement
  remains required. Later windows are allowed to revise earlier structure.
- Deep repair of a very large established subtree may require several bounded passes; a
  single agent is no longer expected to enumerate it completely.

## Revisit when

- real measurements show that 50 cards or 18,000 characters routinely underuse or exceed
  provider context; or
- established deep trees require a separate background queue of affected scopes.
