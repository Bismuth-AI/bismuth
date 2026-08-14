# 0027 — Separate evidence handles from incremental action capabilities

## Status

Accepted.

## Context

Run `20260813T141706Z_bb952c1442` processed 96 documents as `30/30/30/15`, but only the
first window moved documents. Later conclusion requests correctly described a bounded
addressable window while their flattened `inventory` observations exposed committed documents
with indistinguishable `D...` handles. Qwen submitted those reference handles and validation
rejected them as unknown. The same run also exposed an impossible family state: one member was
already in the intended child and another was loose, while atomic F submission required both
and `route_existing` rejected the already committed member.

An established boundary also advertised `create_boundary` and `replace_boundary`. Those
operations cannot be completed from a 30-document incremental window, and `add_sibling`
rejected harmless restatements of the durable axis.

## Decision

- `D...` and `F...` are action capabilities for the current window. Committed evidence outside
  that window uses an `R...` namespace. R handles remain readable but are absent from the plan
  schema's action set and invalid in validation.
- Every fresh conclusion request states the exact submittable D/F units.
- A bounded flat scope exposes only the initial-boundary shape. A bounded scope whose parent
  charter has an established axis and question exposes only `route_existing`,
  `rehome_existing`, and `add_sibling`. Merely having a child directory does not establish a
  managed boundary.
- Incremental plan objects do not accept an axis or question; the host inherits both from the
  durable charter.
- When an F unit contains a member already below its submitted target, that member is an anchor
  and a no-op. Loose members can still move atomically to the anchor. `rehome_existing` may
  repair focused documents from either the boundary parent or its managed direct children.
- Read-only explorers receive `finish_exploration`, so they have a schema-visible exit instead
  of guessing terminal tool names from a later phase.

## Consequences

Tool observations can no longer accidentally grant move authority. Incremental windows do not
offer structurally impossible destructive operations, and a family spanning its correct shelf
and the parent can converge without weakening family atomicity or re-listing an entire existing
boundary. Semantic classification quality remains a separate concern.
