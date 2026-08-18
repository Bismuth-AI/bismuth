# 0018 — Maintenance is four operators, scheduled by drift

## Context

The library grows one document at a time and every structural decision is made with the
evidence that has arrived so far. Measured on one 300-document run:

- The root chose its axis at **24 documents** (`법령의 적용 대상`) and the 276 that followed
  were filed under it. The review at 138 documents said the boundary still held.
- Of 55 folders, **42 have never been divided**, so `due_for_review` returns False on its
  first line and they are never reconsidered. The other 13 need between 4 and 142 more
  documents before their next review. A folder that stops receiving documents is never
  looked at again: `consider` is only reached when something is filed into it or below it.
- One branch reached **seven levels**, six of whose seven segments contain 금융 and three of
  which are the axis said again. Every level was locally justified. Nothing ever saw the
  path.
- `금융` (7 documents, at the root) and `금융업` (6 documents, six levels down) are the same
  subject in two places. Neither folder can see the other.

Two runs of the same code over the same documents in the same order produce different
trees, so none of this is a single bug; it is what an incremental structure does.

Four bodies of work describe this exact situation, and we had reinvented parts of all of
them badly:

**Cobweb (Fisher, 1987)** forms a hierarchy incrementally and, at every insertion, scores
four operations at each node on the path — insert into the best child, create a new child,
**merge** the two best children, **split** the best child by promoting its children — and
applies whichever scores highest. Merge and split are described as reverse operators whose
purpose is *to correct mistakes made on earlier turns*. We have insert, create and a
narrow form of merge (묶어 올리기). **We have no split.** Cobweb can afford to score four
operations at every node because category utility is arithmetic over attribute
distributions; our equivalent judgement is a model call.

**TnT-LLM** builds a taxonomy from a *sample* of summarised documents, refines it
iteratively, and only then classifies the corpus. Our own measurement agrees: 75 documents
produce the same axis and the same four top-level names as 300 (`docs/eval/redesign-lab.md`,
nine runs).

**TaxoAdapt / EvoTaxo** adapt an LLM-built taxonomy to a moving corpus with expand, split,
merge and relabel applied *locally*, explicitly to avoid reclassifying everything.
Microsoft's GraphRAG names the same unsolved piece — deciding when a community has
"drifted" enough to be recomputed, so unchanged ones can be skipped.

**Library practice** is the warning. Academic libraries that reclassified from Dewey to LC
in the 1960s and 70s ran out of budget mid-project and were left with split collections in
two schemes at once. And Ranganathan's diagnosis of why he built facets: a single hierarchy
cannot absorb new knowledge without losing its order, so it eventually demands *complete
revision*. Periodic revision is a property of the structure we chose, not a defect we can
design away.

## Decision

**Maintenance is one closed question at a folder, over four operators, scheduled by drift
rather than by growth alone.**

### The four operators

| | What it does | Have it |
| --- | --- | --- |
| `insert` | file the document into an existing child | 배치 |
| `create` | draw a new class out of the loose pile | 세분화 |
| `merge` | stand existing children under one broader shelf | 묶어 올리기 |
| `split` | dissolve a child and promote its children | **new** |

`split` is the operator that lets a mistake be undone. Without it a level, once drawn, is
permanent: the seven-level corridor cannot shorten and a folder named after one law keeps
its 26 documents for ever. It moves folders and documents up one level and deletes the
node; no document changes the folder it is in, only the path above it.

Asking these as one closed choice rather than as separate open questions follows 0014 and
0017: code enumerates what is structurally possible at this folder, the model picks one.

### Drift, not only growth

`due_for_review`'s doubling rule is a *schedule*, and it is the only schedule we have, which
is why 42 folders are frozen. A second schedule is added: code measures how far a folder's
own evidence has moved since it was last judged — the spread of the vocabulary its cards
carry — and asks again when it moves. This is counting, not judging, and it is
corpus-neutral: it reads whatever words the cards happen to contain.

Cluster-drift work triggers split and merge on exactly this signal, from the observation
that in a settled structure the spread inside a cluster shrinks as it is refined.

### Whole-collection redesign stays, and is bounded

Order dependence is inherent to incremental clustering; the standard mitigation is
post-processing, and a whole-tree pass *is* that post-processing. It is not an admission
that the incremental path failed.

It is bounded by asking at folder granularity:

- **design** from a sample — measured sufficient at 75 documents, so O(1) in collection size
- **assign** one closed question per folder plus one per loose document — O(folders), and
  folders grow far more slowly than documents
- **move** folders whole, so no document changes the folder it sits in and no subtree is
  re-divided
- **one transaction**, all or nothing. A redesign that stops half way leaves the split
  collection the libraries were left with.

The same pass names folders that are unsound, and being named schedules a review the way
drift does. This is what breaks the freeze: the global view is the only place from which
`금융` and `금융업` can be seen together.

## Consequences

We accept that structure is rewritten periodically and forever, and we buy the right to
draw a level early and badly. Everything before this treated a drawn boundary as nearly
permanent, which is why the prompts argue so hard: the cost of a wrong name was unbounded.
With `split` and a redesign that is O(folders), a wrong name costs one later operation.

We pay for a drift measure on every folder. It is arithmetic over cards we already hold.

We do not get a structure that stops needing revision. Ranganathan's argument says a single
hierarchy cannot have one, and the filesystem is the product (0001), so a single hierarchy
is what we have. Facets remain unexplored and are a SPEC-level question, not an ADR one.

An LLM reading this library traverses it and can judge a page after opening it, so it
recovers from an imperfect tree in a way a ranked retrieval cannot. The target is a tree
that is navigable and honest, not one that is right.

## Revisit when

- A drift schedule fires so often that maintenance dominates the run, or so rarely that
  folders still freeze. Both are measurable in `scripts/guards.py`.
- `split` and `merge` oscillate on the same folder across redesigns.
- A collection outgrows a 75-document sample — that is, the sample stops producing the
  axis the whole corpus produces. Measured, not assumed.
- Facets are taken seriously, which would supersede the single-hierarchy premise this
  record is written under.
