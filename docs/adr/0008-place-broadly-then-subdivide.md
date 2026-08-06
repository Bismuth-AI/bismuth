# 0008 — Place broadly, then subdivide

**Status:** accepted
**Amends:** [0007](0007-agentic-placement.md)

## Context

[0007](0007-agentic-placement.md) made filing one model call against the current
folder tree: read the notes, pick an existing folder or invent one, move the file.
Structure emerges from placement alone. Its stated revisit condition was *"drift
shows up in practice on a real corpus"*.

It has. On eleven documents:

- One folder, named from the first document that landed in it, absorbed six —
  including three that were not about its subject. The name was `생성형AI_RAG`;
  four of the six were education, psychology and philosophy papers.
- Three documents were parked with the model saying, in its own words, that it had
  assigned "the nearest folder, temporarily" because nothing fit.
- A folder was named after one document's exact topic, and then pulled in
  neighbours that matched the name rather than the subject.

Eleven documents. The mechanism scales the wrong way: the earlier a document
arrives, the more it decides.

The diagnosis is not that placement judges badly. It is that **placement is the
only operation there is.** A first decision made from one document is necessarily
made from thin evidence, and 0007 gives it no way to ever be revised. Everything
after inherits it.

Returning to [0005](0005-humans-choose-the-axis-order.md) — discover facets across
the corpus, have a human order them, derive paths — is not the answer either. It
requires seeing the whole corpus before filing anything, and a human decision
0007 was right to remove.

A librarian does neither. They do not read the whole collection before shelving
the first book, and they do not refuse a book because no shelf matches. They put
it in the broadest class that fits, **and when a class grows they split it.**

## Decision

**Filing has two operations, not one.**

| | |
|---|---|
| **Place** | Put a new document in the folder that fits, at the broadest level that is still right. Create one when nothing fits. |
| **Subdivide** | When a folder has grown, split it into sub-folders along whatever distinction its own contents suggest. |

The second is what makes the first survivable. With subdivision, an early
placement is **provisional** — wrong-but-broad is a recoverable state, and the
tree stops being a function of arrival order. Without it, every placement is
permanent and the first documents write the structure.

This also reframes what 0007 called its non-user. 0007 said a team needing "a
fixed, human-designed top-level split" is not this tool's user, and read the
alternative as a personal, growing collection. The distinction does not survive
the librarian framing: a library is not organised differently because it is large,
and its principles do not change with who walks in. Scale changes how often you
subdivide, not what the tool is.

**Subdivision is judged, not triggered.** No count, no threshold, no rule of the
form "over N documents, split". The right size for a folder depends on how
separable its contents are, and a constant would be a heuristic tuned on whichever
corpus we happened to measure — see [SPEC.md §6.1](../../SPEC.md). Numbers exist to
check the result from outside, not to make the decision inside.

## Consequences

- **The first placement no longer has to be right,** only broad enough to be a
  recoverable mistake. This is a weaker requirement than 0007 placed on it, and
  weaker requirements are what the model can actually meet.
- **Documents move after they were filed.** 0007's tree only grew; this one is
  rewritten in place as it learns. Every such move is journalled and reversible
  like any other change, so "the model reorganised something" is inspectable and
  undoable rather than a fact you discover later.
- **Order-dependence becomes measurable.** Ingest a corpus twice in different
  orders and compare the trees; that is a check no other property of this system
  offered. It is also expected to fail today, which is the point of writing it
  down before fixing it.
- **Folder notes have to be rewritten when a folder is split,** and the sub-folder
  notes have to distinguish siblings — the note is what the searcher reads to rule
  a folder out ([SPEC.md §3.6](../../SPEC.md)).
- **Cost moves.** 0007 was O(1) per document in corpus size. Subdivision reads a
  folder's cards, so it is O(folder), paid when a folder grows rather than per
  document. Whether that is affordable at real scale is not yet measured.

## Revisit when

Subdivision proves insufficient on its own — specifically, when the trees produced
from two different ingest orders stay dissimilar even though every oversized folder
has been split. That would mean the problem is not granularity but the top-level
distinction itself, and the part of [0005](0005-humans-choose-the-axis-order.md)
worth recovering is its answer to *that*: some axis choices are not recoverable
from the documents. Do not bring back the human axis decision before measuring
this; 0007 was right that it was a wall.
