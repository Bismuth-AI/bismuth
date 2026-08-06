# 0007 — Place agentically: the model reads the folder tree and picks or creates

**Status:** accepted, amended by [0008](0008-place-broadly-then-subdivide.md)
**Supersedes:** [0002](0002-two-loops.md), [0003](0003-derived-paths-not-chosen-folders.md), [0005](0005-humans-choose-the-axis-order.md)

## Context

The first design treated filing as *faceted classification*: discover a fixed set
of facets (project, year, doc type) across the whole corpus, ask a human which
facet goes on top, extract each document's facet values, and compute the folder
path by arithmetic. A second "slow loop" watched for structural pressure and
proposed reorganisations for the human to approve.

It worked, and it was wrong for this product. The machinery it required —
discovery, an axis-order decision, a per-corpus taxonomy, a proposals system, an
inbox where documents waited until enough of them accumulated — was a wall in
front of a tool whose whole pitch is that it removes work. Worse, it was built on
a claim the product did not actually need: that the tree shape is a single global
decision a human must make once. For a personal or growing collection, it is not.

The user's own spec, stated plainly, was the correction: *put a document in, it
gets filed; put the next one in, it is filed by looking at the folders that already
exist; folders are created as needed, at any depth; that is all.*

## Decision

Filing is one model call against the current folder tree. No facets, no axis
order, no discovery, no arithmetic, no slow loop.

1. Parse the file; the model produces a card (what it is, what it is *about*).
2. Show the model every existing folder path with its one-line note, plus the card.
3. The model returns a folder path — an existing one, or a new one at any depth.
4. Move the file there, write the sidecar, and note a brand-new folder.

The first document creates the first folder. Every document after it is filed by
looking at what earlier ones built. Structure emerges; it is never declared.

> **Amended by [0008](0008-place-broadly-then-subdivide.md).** Placement alone made
> every first decision permanent, so the documents that arrived first wrote the
> structure. Filing now has a second operation — subdivide a folder once it has
> grown — which makes a first placement provisional.

**Drift — the real cost — is handled by showing the whole tree every time.** The
old design prevented drift with determinism (arithmetic gives the same path every
time). This design prevents it by making the model *see* the existing folders and
telling it, firmly, to reuse before it invents. An existing folder that fits beats
a new one that fits slightly better. This is weaker than arithmetic and strong
enough, and it is what buys the simplicity.

## Consequences

- **Scale is fine, and the old "5000 impossible" was a conflation.** Placing one
  document reads folder *notes* (one line each), never the documents — O(1) in the
  corpus size even at hundreds of folders. Only rebuilding a global taxonomy was
  ever expensive, and that step no longer exists.
- **Depth is unbounded and per-document.** `법무/계약/대한물산/2023` and a flat
  `메모` can coexist; the model picks what fits the existing shape.
- **The human decision is gone.** No axis order to choose. This trades a
  human-perfected structure for a zero-effort evolving one.
  *(0008: the second sentence of this bullet originally read that the trade suited
  "the personal and growing case this tool is really for", and that a team needing a
  fixed top-level split was not this tool's user. That framing is withdrawn — a
  library is not organised on different principles because it is large.)*
- **~2000 lines deleted:** facet/taxonomy domain and service, discovery, the
  pressure/proposals slow loop, arithmetic path derivation, the reconcile step,
  and the discover/review CLI and API surface.
- **Refusal survives.** The model may return null, or low confidence, and the
  document waits in `_inbox`. That is the one case a document is not filed, and it
  stays loud on purpose.

## Revisit when

Drift shows up in practice on a real corpus. The mitigation is not to bring back
arithmetic; it is a periodic "tidy" pass that reads the folder notes (not the
documents) and consolidates near-duplicate folders. That is agentic too, and cheap
for the same reason placement is. It is deliberately not built yet — measure first.
