# 0024 — Symmetric window evidence and progressive acceptance

## Status

Accepted.

## Context

Run `20260813T073153Z_dbe8265209` filed and carded all 86 documents but moved none.
The planner received at most 30 addressable documents, while each semantic critic could
page all 86 root cards. Critics therefore rejected candidates for omitting documents the
planner could neither read nor move. Accepted `submit_review` calls also failed to stop the
critic, and three planner corrections ended at the 4,096-token ceiling without a tool call.
Compact organizer cards showed an empty summary even though the catalog held a populated
card because the tool read only sidecar frontmatter.

## Decision

- Planner and critics share one evidence capability: current movable arrivals plus
  documents already committed below established shelves. Unprocessed loose backlog and
  deferred failures are excluded.
- A new upload never replays deferred IDs implicitly. Thirty fresh pending documents remain
  thirty fresh documents; the upload tail still flushes immediately.
- Organizer cards load title, type, topics, and summary from the durable catalog. Sidecar
  metadata remains the fallback for embedders without a catalog.
- An accepted `submit_review` stops its critic immediately. A rejected `submit_plan` is not
  a completed conclusion and cannot suppress the required conclusion-tool retry.
- Deterministically grounded exact-title and subordinate-instrument family partitions are
  not vetoed merely because a critic prefers a uniform law/decree relationship.
- For `create_boundary` only, a finding that maps to complete cited sibling moves may remove
  those moves while leaving at least two uncited validated siblings. Indivisible operations
  such as `replace_boundary` never receive partial acceptance.
- Agent prose guards retain short suffix and whitespace detection and add exact recurring
  eight-word sequence detection. A guarded partial prose response advances to the Agent Kit
  conclusion phase instead of failing the maintenance window.

## Consequences

The first window can create an initial boundary without being judged against future hidden
arrivals. Later windows see that committed structure and can route their own documents into
it. A single questionable target no longer discards unrelated valid siblings, while all
filesystem changes remain deterministically validated and atomic.
