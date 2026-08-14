# 0033 — Recover incremental blind spots only after the sibling contrast is complete

## Status

Accepted. Extends ADR-0030 and ADR-0032.

## Context

Incremental placement sees only the shelves that exist when one document arrives. In run
`20260814T122226Z_80c70badb6`, `할부거래법` was accepted by `금융` before a consumer shelf
existed. The final loose-document router also retried the same rejected family after unrelated
documents moved. One `산업기술단지` audit first returned `STAY`; a later identical-topology
retry happened to return all `BELONG` and moved the family. Repetition converted a conservative
review into eventual false acceptance.

## Decision

- A loose atomic family is reviewed at most once for one exact set of sibling signs during an
  upload finalization. Its key is `(topology, stable source paths)`, not request-local D-handles.
- The closed loose choice is an existing F-handle or `NEW_SIBLING`. The latter explicitly means
  that the natural parent is absent; nearest-fit routing is invalid.
- After class discovery and loose recovery, only filenames from the just-finished upload are
  reviewed against the now-visible top-level sibling contrast. Older documents are outside this
  automatic correction scope.
- Moving an already filed family requires three independent positives: a different sibling
  choice, `MOVE` in a current-versus-proposed comparison, and `BELONG` for every atomic member.
  Ambiguity keeps the current filing.
- Organization administration and document-family invariants remain hard validation boundaries.

## Evidence and consequences

The same deterministic 150-file sample changed from root 26 with four shelves
(`20260814T122226Z_80c70badb6`) to root 28 with six coherent shelves
(`20260814T125315Z_24e138624f`). `할부거래` moved from finance to consumer protection,
`1인 창조기업` finished under small business, institutional `직제` families stayed at root, and
`산업기술단지` stayed loose after its first rejected membership audit. The second run had 29
loose routing calls, four `NEW_SIBLING` answers, and zero duplicate topology/family routing keys.
The design intentionally prefers a few more root documents over repeated chances to force-fit.
