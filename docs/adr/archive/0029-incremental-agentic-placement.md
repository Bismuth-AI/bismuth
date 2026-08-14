# 0029 — Pivot from arrival windows to incremental agentic placement

## Status

Superseded by ADR-0030. Superseded ADR-0015 through ADR-0028 for the automatic ingest path. Those
records remain as the history and implementation notes of the retired bounded-window
organizer; their diagnostics and safety work may still be reused.

## Context

The bounded planner received up to thirty arrivals plus an existing tree, proposed a
multi-document shadow plan, and sent the exact candidate through two semantic critics.
Several days of raw-log debugging improved observability, family closure, path safety,
context compaction and terminal phases, but did not make the central SLM task reliable.

Run `20260813T153650Z_1fa55e2be1` made the mismatch concrete:

- the first window created useful top-level folders, but later windows proposed no-op
  moves for documents already in those destinations;
- normalization accepted an empty boundary and execution then failed reconstructing a
  schema that requires one move;
- a critic described a placement as correct and said there was no blocking finding while
  simultaneously returning `blocking=true`;
- an empty candidate still entered membership review, which made 46 `related` calls over
  59 turns despite active-context compaction;
- one window failure affected the status and progress of unrelated later arrivals.

The earlier file-by-file harness produced more usable trees because every decision was a
small classification against the structure that already existed. Its weakness was that
the choice was not an evidence-seeking agent and early decisions could not open a useful
new shelf safely.

## Decision

Automatic ingestion is incremental and document-scoped.

1. Parse and card documents independently; bulk uploads may prepare cards concurrently.
2. File prepared documents serially in upload order.
3. For one readable document, create a fresh Agent Kit transcript containing only:
   - compact current folder paths and routing signs addressed by `F...` handles;
   - a small corpus-neutral related-card shortlist addressed by `D...` handles;
   - `inspect_folder`, `inspect_document`, and terminal `finish_placement` tools.
4. `finish_placement` can choose an existing shelf, keep the document at an existing
   parent/root, or create one direct child below an existing parent.
   It may also name related `D...` companions that are still loose directly at that same
   parent. Those companions move into the chosen child with the arriving document; it
   cannot pull documents out of another established sibling.
5. New folder names are single semantic path segments. The host validates the handle,
   exact sanitized name, collision state and parent. The tool does not mutate files.
6. New-folder purpose is part of the accepted terminal action. Folder creation, original
   move, sidecar and sign are one journal transaction; no second model call rewrites the
   decision immediately afterwards.
7. Existing non-root family placement remains a deterministic fast path grounded in the
   current colocated sidecars.
8. If the agent ends without an accepted terminal action, placement falls back to the
   older closed-choice walk through existing signs. This is a circuit breaker, not the
   normal architecture.

The 30-document queue, tail flush, pending/deferred maintenance checkpoint and automatic
global planner/critic are removed from upload, batch and inbox-scan workflows. The
organizer package remains available for diagnostics and future explicitly triggered local
repair, but arrival alone cannot run it.

## Consequences

- A single SLM call chain reasons about one document rather than a combinatorial partition.
- Every next document observes the real folder produced by the previous one.
- One failed placement cannot invalidate or delay unrelated documents in the upload.
- First-document folders are allowed but provisional: the prompt requires a reusable class,
  while root remains a valid destination when no class is grounded.
- A later document can grow a useful local shelf out of related loose parent documents, so
  early root placements are recoverable without a global maintenance pass.
- The normal path no longer needs planner/critic agreement, maintenance windows, no-op plan
  normalization, or persisted retry windows.
- Order independence is still an external quality criterion. Incremental placement alone
  may not repair an early bad boundary, so the next architecture step is event-triggered
  local repair scoped to one implicated parent, never a fixed-size global batch.

## Revisit when

Raw runs show that document-scoped agents still create duplicate siblings or cannot repair
early broad shelves. Add local repair only for a concrete event such as a split family,
new-document/sign conflict, or duplicate sibling. The repair must receive one parent and a
closed related-document capability set; do not restore fixed 30-document windows.
