# 0017 — Maintain between bounded arrival windows

**Status:** accepted
**Amends:** [0015](0015-agentic-shadow-planning.md), [0016](0016-resumable-maintenance.md)
**Execution budget amended by:** [0018](0018-addressable-agent-context.md)

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
at most 30 cards and at most 18,000 card characters. These are execution ceilings, not a
rule that the collection should be divided. The agent sees:

- the complete bounded `arrivals` window;
- the current tree and stable folder boundary notes produced by earlier windows; and
- bounded inventory pages only for folders affected by the new arrivals.

This is a host-enforced capability boundary, not a prompt request. `submit_plan` can move
only the current window. Read tools expose the current window plus documents already
committed below established shelves; loose root backlog and deferred failures remain
hidden. Raw paths cannot bypass that boundary. Planner and semantic critics receive the
same evidence capability. Whole-vault critic visibility was removed because it allowed a
critic to reject omissions the planner had no authority to repair.

The arrival tool can return its window only once. The original implementation also gave
the primary planner five read calls and the verifier four. ADR-0018 removes that shared
call counter: active context is now compacted into an addressable observation archive and
identical non-progressing calls trigger the conclusion phase.

After a window's shadow plan validates and applies, the next documents are placed against
that updated tree. Thus a 300-document upload normally performs ten bounded maintenance
passes, and document 31 sees the structure learned from documents 1–30.

Review is not completion. Focus documents still loose at the reviewed boundary remain in a
durable `partial` checkpoint. Deferred documents do not join a later new-arrival window
implicitly. ADR-0026 permits only an exact grounded family mate to re-enter with a current
arrival, displacing unrelated pending work to preserve the 30-document ceiling. There is no manual structure-retry command. Filing into one
already-existing child is valid even though creating a new boundary still requires at least
two reusable siblings. A manual action on a legacy false-success checkpoint seeds the loose
root documents first, not the entire catalogue.

Full 30-document windows run during ingest. When one HTTP upload, scan, or background batch
ends, its final 1-29 arrivals flush immediately as one last bounded window. Thus 153 files
produce five 30-document windows and one 3-document window. The independent
18,000-character safety ceiling may close a window earlier.

If one window fails, file ingest continues and later IDs join the same durable backlog.
Automatic maintenance does not repeatedly hit the same broken model during that batch.
After configuration is fixed, retry consumes the backlog in the same bounded order and
updates the tree between windows. Source documents are never reparsed.

Window packing treats a grounded document family across current and prior workflow state as
indivisible. If including a late or prior family mate would exceed 30, unrelated pending IDs
move to the next window. Earlier deferred IDs otherwise remain diagnostic state rather than
automatic work.

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

- real measurements show that 30 cards or 18,000 characters routinely underuse or exceed
  provider context; or
- established deep trees require a separate background queue of affected scopes.
