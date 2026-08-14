# 0019 — Review the exact candidate and separate structural rights

**Status:** accepted
**Amends:** [0015](0015-agentic-shadow-planning.md),
[0017](0017-incremental-arrival-windows.md),
[0018](0018-addressable-agent-context.md)

## Context

The first agentic planner improved the broad semantic axis of a real legal vault, but
still produced siblings at different abstraction levels, list-like catch-alls, overlapping
top-level areas, and split document families. Deterministic validation correctly rejected
unsafe paths and incomplete handles, but could not reject a grammatically valid bad
taxonomy.

The planner also asked a verifier to review a free-form description before separately
calling `submit_plan`. The reviewed description was not the object later applied. A simple
move into an existing child used the same boundary schema as creating or replacing an axis,
so ordinary routing could unnecessarily rewrite durable boundary state.

Finally, non-root scopes were deduplicated only inside one in-memory maintenance drain.
Another drain could review the same unchanged evidence again.

## Decision

Structural plans declare the least powerful operation they require:

- `route_existing` moves documents only into existing direct children and cannot alter
  boundary state;
- `create_boundary` creates the first sibling boundary at a flat parent;
- `add_sibling` reuses an established axis and adds a new direct answer;
- `replace_boundary` changes an established axis and must account for every document below
  its old children.

The root pass is the global architect and may change only the root boundary. A non-root
pass is a local organizer and may change only its exact assigned scope.

`submit_plan` validates the actual typed candidate without changing its targets or
membership. The host then passes that same validated object, its current boundary state, representative cards, and
request-local handles to two fresh critic contexts. There is no separate prose plan
to verify. Each critic inspects the vault and submits structured findings through
`submit_review`.

The critics have separate responsibilities:

1. boundary coherence — sibling overlap or containment, abstraction-level mismatch, mixed
   axes, list-like catch-alls, and duplicated established boundaries;
2. membership coherence — related families split across shelves, representative-only fits,
   forced narrow placement, and documents safer at the parent.

The boundary critic cannot anchor the membership critic because their contexts are isolated.
Code may surface possible relatives from shared open card metadata, but this is evidence
retrieval only. It never decides that two documents are a family or that a boundary passes.
The critic may not reject from name length, punctuation, folder counts, a fixed domain
taxonomy, or forced complete coverage.

A blocking review returns concrete findings to the planner. One revised complete candidate
is allowed and reviewed from scratch. A second rejection preserves the current tree and
must end explicitly with `finish_no_change`; no partial candidate is applied.

Reviewed non-root scopes are stored in the durable maintenance checkpoint with a fingerprint
of their documents, direct children, and boundary state. An unchanged fingerprint suppresses
repeat review across drains. New evidence changes the fingerprint and reopens the scope.

## Consequences

- The semantic review now covers exactly what deterministic code would execute.
- Existing-folder routing cannot silently rewrite an axis or folder signs.
- Global and local agents have non-overlapping mutation authority.
- Semantic defects become typed, logged counterexamples rather than unstructured advice.
- Review adds an isolated model run for every deterministically valid candidate. This is
  intentional: invalid candidates are still rejected before spending critic context.
- Shared metadata can miss a real family or surface a false relative. It only helps the
  critic choose what to inspect, so the final decision remains corpus-neutral model judgement.
- Same-model review can still share blind spots. Isolation, exact-object review, hostile
  questions, and concrete evidence reduce anchoring but do not prove semantic correctness;
  real-vault evaluation remains required.

## Revisit when

- traces show the critic repeatedly misses one class of counterexample;
- related-card retrieval overwhelms the critic with weak evidence;
- one candidate routinely needs more than one repair; or
- a separate configured reviewer model has measured quality or cost benefits sufficient to
  reconsider ADR-0012's single-model contract.
