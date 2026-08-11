# 0009 — The model proposes; the library validates

**Status:** accepted
**Amends:** [0008](0008-place-broadly-then-subdivide.md)

## Context

The first 300-document legal corpus exposed failures that prompt wording could not
contain:

- Eight of twenty-one recorded axes were English explanations truncated to exactly the
  schema's 40-character maximum. The fragment was then presented as `THE AXIS HERE` on
  every later ingest.
- A boundary review assigned `기술사법` to two groups. The first move succeeded and the
  second failed because the source no longer existed. The transaction rolled back, but
  maintenance made a safely filed document appear to have failed ingest.
- 848 emerging-class questions were asked for 300 documents. Most ancestor calls saw the
  same loose pile as the previous call; only their subtree count had changed.
- The resulting tree had 62 folders and depth five where the human reference used two
  axes and depth two. A short label was not enough to keep siblings on one real question.

The model is the right place to judge meaning. It is not the right place to enforce
uniqueness, path safety, cardinality, or transactional consistency.

## Decision

**A model response is a proposal, never a filesystem command.**

Before operations are built, the complete maintenance plan is rejected if:

- its axis is prose rather than a short label;
- an ancestor already consumed the axis;
- a class repeats an ancestor or another proposed name;
- a document is unknown or appears in more than one class;
- a sign would point at one document; or
- one class merely moves the entire loose pile one level deeper.

Axis state stores both a short label and the question every child answers. Human-facing
explanation remains in the note body and never becomes machine classification state.
Schema-2 notes whose axes are known truncated prose are read as undivided rather than
carrying corrupt state forward.

Placement commits independently from maintenance. If maintenance fails, its own
transaction rolls back and the document remains filed, carded, and searchable.

On arrival, an emerging-class question is asked only at the folder whose direct loose
pile changed. Ancestors are reviewed only when their subtree evidence makes the existing
boundary due. A descendant arrival changes an ancestor's evidence, but it does not add a
new loose document there.

When first choosing an axis, the model considers alternatives and compares them by
general navigation properties: exclusion power, mutual exclusivity, repeated work below
each child, and stability as the collection grows. No domain axis is built in.

## Consequences

- Invalid plans cost model tokens but perform zero filesystem operations.
- A maintenance failure no longer erases the ingest completion event.
- One-document folders are rejected as a representation invariant derived from
  [SPEC.md §3.4](../../SPEC.md), not from a tunable size threshold.
- Ancestor calls fall with tree depth because unchanged loose piles are not re-judged.
- Existing schema-2 vaults with valid short axes migrate normally. Truncated explanatory
  axes are deliberately forgotten and may be rebuilt from the collection.
- Axis comparison remains a model judgement. Its quality must be measured with shuffled
  ingest orders, not asserted from the prompt.

## Revisit when

Validated plans still produce order-dependent trees on multiple corpora. That would mean
the missing operation is not safer subdivision but a broader merge, subtree move, or
whole-axis replacement plan. Add those as explicit validated operations; do not weaken
the proposal boundary.
