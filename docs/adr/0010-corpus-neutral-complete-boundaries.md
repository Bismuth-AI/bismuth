# 0010 — Corpus-neutral prompts; complete boundary replacement

**Status:** accepted
**Amends:** [0009](0009-model-proposes-the-library-validates.md)

## Context

A legal corpus revealed two different kinds of contamination.

First, production prompts named candidate legal axes. Even when intended only as format
examples, those labels made the corpus an invalid evaluation of whether the model could
discover an axis from the documents. Fixed Korean root text and an English opening-word
blacklist added language assumptions without making semantic validation reliable.

Second, `Review` could change the recorded axis and create new children, but it neither
read documents below existing children nor retired the old child tree. A folder therefore
kept children from the old axis beside children from the new one. The operation called a
boundary replacement was only an additive extraction from the loose pile.

## Decision

Production classification prompts contain no example domain, taxonomy, axis value,
organisation, date hierarchy, or expected folder name. Historical examples remain in
tests and decision records because they explain failures; they are never sent to a model.

Pure code validates only facts it can know without understanding the corpus: unique file
handles, complete membership for replacement, no duplicate assignment, path safety,
cardinality, spent ancestors, and atomicity. It does not guess whether prose is a label
from word counts, punctuation, or an English blacklist.

Semantic proposals pass a second model judgement that receives only the current
collection and proposal. It verifies that the axis names one property, the question asks
that property, sibling names answer it, siblings are mutually exclusive, and the boundary
helps navigation. This is judgement, not a corpus-specific rule.

A negative `Review` is a complete subtree replacement:

1. Read every card and current path below the reviewed folder.
2. Give each document a short request-local handle and require every handle exactly once
   in the proposed sibling groups.
3. Refuse a subtree containing an uncarded file or human-managed descendant.
4. Stage every original and sidecar in Bismuth's private state directory.
5. Retire all old descendant notes and directories.
6. Recreate the proposed direct children, reusing names when proposed, and place every
   document under the new boundary.
7. Write child notes and the parent's new axis in the same journal transaction.

Staging removes move cycles and lets rollback restore every original path. Replacing a
boundary deliberately flattens its old lower classification; subsequent evidence may grow
new lower boundaries. Preserving descendants from an obsolete axis would preserve the
contradiction.

Between scheduled reviews, loose documents may also be routed into an existing direct
child without changing the axis. The model may leave any document loose, target names must
exactly match managed direct children, an independent audit rejects forced fits, and the
document plus sidecar move in one journal transaction. This closes the gap where maintenance
could create a new sibling but could not reuse the correct shelf already present.

Review has two stages. The first returns only three checks that directly determine whether
the current boundary holds. A complete `Replacement` plan is requested only when a check
fails. Neither response contains a free-form rationale: identifier completeness and
duplicate membership are deterministic code checks, while the independent semantic audit
returns only the checks that gate mutation. This avoids spending output tokens narrating
or recounting work whose result is already present in structured fields.

Charter schema 5 marks every older divided charter for immediate complete review. The
semantic audit covers the current boundary's full subtree and a failure enters the same
complete-replacement path as a failed Review; it is not merely logged and left in place.
An old boundary is evidence, not trusted state.

Adding a later sibling also audits the whole direct-child boundary, not the proposed
class in isolation. Request-local document handles are remapped by their actual paths so
existing child memberships and the proposed loose-document memberships can be checked
together without exposing content hashes.

Folder notes now generate and persist only one positive `purpose` routing sign. Legacy
`holds` and `answers` remain readable for compatibility but are not used by placement and
are omitted on the next managed write. Replacement change control returns only whether
the proposal fixes the observed failure and materially improves navigation; a separate
"worth relearning" verdict duplicated those two checks and could preserve a boundary
already proven invalid.

## Consequences

- A legal corpus can test discovery again because the prompt no longer supplies legal axes.
- Archives in other languages no longer inherit Korean classification text or an English
  lexical validator.
- Review input costs scale with the reviewed subtree, but a boundary that still holds
  returns only three booleans. The full replacement output is generated only after failure.
- A semantic auditor adds one reasoning call to proposals that would otherwise mutate the
  tree and once while migrating a legacy boundary.
- An invalid or incomplete replacement moves no files.
- Parent-folder renaming remains a separate journalled operation; a boundary replacement
  changes children and axis, not the reviewed folder's path.

## Revisit when

The all-at-once pass was replaced by bounded complete maintenance in
[0011](0011-bounded-maintenance-and-stable-signs.md). Revisit the packet/reduce strategy
when quality measurements show semantic loss, while preserving complete membership and the
single-transaction boundary. Do not reintroduce corpus examples or silent sampling.
