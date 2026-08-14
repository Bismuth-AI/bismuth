# 0025 — Make families visible and repairable across incremental windows

## Status

Accepted.

## Context

Run `20260813T083201Z_c5f1773768` processed 195 documents as six 30-document
windows and one 15-document tail. The first window created some root shelves, but later
windows repeatedly failed deterministic family validation and left 132 documents at the
root. The raw request, response, tool-result, and validation artifacts showed three
contract gaps rather than a transport or context-isolation failure:

- arrivals were family-cohesive, but the planner was not told which handles formed each
  grounded family;
- rejection text named family titles but not the movable handles and their current/final
  direct shelves;
- a later window could discover an earlier misroute but a leaf-scoped `add_sibling` plan
  could not repair the established parent boundary.

The same run also subdivided a nine-document shelf immediately after the first root pass,
then selected a two-document leaf in a later pass. Review state was fixed for the entire
drain, so new evidence did not reopen the parent boundary. This produced premature
document-type shelves and made later family repair impossible. Most wall time was serial
LLM waiting: 470 structured calls plus 218 agent-chat calls, with about 25 minutes of summed
provider latency in a roughly 27-minute run.

## Decision

- `arrivals` labels every grounded multi-document family with a stable family identifier
  and lists all movable handles in that family.
- Family validation remains strict. Rejections return each movable handle with its current
  and proposed final direct shelf so the next tool call can be corrected exactly.
- `add_sibling` may atomically move current-window documents from an AI-managed existing
  direct child while adding a sibling. It still cannot cross a human-managed ancestor,
  move a no-op document, or empty an existing boundary value.
- A flat shelf is eligible for automatic subdivision only after it has 30 direct documents.
  When arrivals occur below an established managed boundary, the parent boundary is the
  review scope. Reviewed scopes are tracked by fingerprints recomputed between windows;
  changed evidence reopens the boundary.
- Batch extraction and card preparation may run concurrently up to the configured LLM
  concurrency. Filing, tree mutation, and maintenance checkpoints remain ordered and
  sequential.
- Organizer agents receive smaller turn budgets, may call the tree once, and may not repeat
  an identical tool call. This bounds Qwen repetition without weakening validation.

## Consequences

Later windows can see and repair a family that spans existing structure and current
arrivals without widening the 30-document movable window. Small shelves remain broad until
there is enough direct evidence to justify another layer. Preparation latency overlaps,
while the incremental guarantee that document 31 sees the structure learned from documents
1–30 remains intact.

Existing failed vault state is not silently rewritten. The change applies to a new ingest
run after restart; any already-created structure remains ordinary filesystem state.

