# 0011 — Stable routing signs; bounded complete maintenance

**Status:** accepted
**Amends:** [0010](0010-corpus-neutral-complete-boundaries.md)

## Context

Managed folder notes were regenerated after ordinary document arrivals. That made a
routing boundary drift with its inventory, spent a reasoning call per file, and allowed
an overlong `purpose` to fail an ingest after the original and sidecar were already safe.
The prompt requested 220 characters, but model obedience is not a safety boundary.

Complete review also serialised every card and descendant sign below a boundary into one
request. At the root of a 260-document vault the prompt exceeded a 65,536-token model
context. Because the review schedule was not re-armed, every later arrival retried the
same impossible request.

## Decision

A folder `purpose` is an intensional routing contract: what may belong behind the sign.
It is not an inventory summary. Ordinary additions, deletions, and moves preserve an
existing managed purpose. New and replacement boundaries write their signs atomically
with the structural transaction. Missing notes may be created, but note failure after a
file commit is logged separately and never changes ingest success.

Model prose is untrusted. New purposes are whitespace-normalised in code. A value longer
than 220 characters is not retried or blindly truncated; an existing sign is retained,
and a new class falls back to its already validated folder name.

Every automatic maintenance prompt has a 32,000-character preflight budget. Small
boundaries keep the direct single-call path. Larger work is isolated into stages:

1. Review every direct sign and every document card in bounded evidence packets.
2. Combine boolean review checks fail-closed.
3. If replacement is required, produce membership-free boundary sketches per packet and
   reduce them to one axis and sign set.
4. Assign every request-local document handle to those fixed signs in bounded packets.
5. Audit boundary semantics and change-control in bounded packets.
6. Validate exact membership in code and apply the complete replacement in the existing
   single journal transaction.

No document or direct sign is silently sampled. A pathological single legacy card may be
prefix-compacted only after retaining its handle and current path; original document bytes
were never maintenance input. Very wide incremental boundaries are deferred to boundary
review instead of sending an over-budget request. No domain taxonomy, language, document
count, or fixed sibling-count target is built into the packet strategy.

The deterministic staged pipeline is used instead of the general Agent Kit loop because
the mutation contract is fixed, completeness is mechanically checkable, and no autonomous
tool choice is needed. Agent Kit remains the right abstraction for interactive navigation
and user-approved reorganisation.

## Consequences

- File arrivals no longer spend a charter-generation call or rewrite learned signs.
- A malformed or unavailable folder-note response cannot produce a false ingest failure.
- Review and replacement request size is bounded independently of subtree size.
- Large replacements use more calls, but each has isolated context and every document is
  accounted for before mutation.
- Packet reduction is lossy semantic compression, so structure quality must still be
  measured; file completeness and transaction safety remain deterministic.
