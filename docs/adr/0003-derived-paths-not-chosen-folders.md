# 0003 — Derive paths from facet values; never let the model pick a folder

**Status:** accepted

## Context

"Where does this document go?" has an obvious implementation: show the model the
folder tree, show it the document, ask it to pick.

It works. It works on document 1, on document 10, and in every demo.

It fails invisibly at scale, and the mechanism is worth spelling out because it is
not a prompt problem and no amount of prompt engineering fixes it. The model's
context differs between calls — a different tree, different neighbouring folders,
a different sample of what it has seen. So it picks `projects/apollo/contracts`
for document 1 and `contracts/apollo` for document 300, and both choices are
defensible in isolation. Six months later the tree looks organised and is
incoherent, and **nobody notices**, because there is no moment at which anything
appeared to go wrong.

That is the worst class of failure: no symptom, no error, no bug report. Just
retrieval that quietly underperforms and a vague sense that the AI is not very
good.

## Decision

**The model never picks a folder. It answers questions about the document, and the
path is arithmetic.**

```
model:        "what project is this?"     -> "Apollo"
model:        "what year is this?"        -> "2023"
pure function: path_for({project, year})  -> Apollo/2023
```

[`Taxonomy.path_for()`](../../src/bismuth/domain/facet.py) is a pure function with
no I/O, no model, and a test that calls it fifty times and asserts one distinct
result. Document 300 lands where document 1 landed because it cannot do otherwise.

Three consequences of the split fall out immediately:

- **A wrong answer becomes visible.** "project=Apollo" attached to a Zephyr
  contract is an extraction error a human spots in two seconds. "The model thought
  this folder felt right" is not checkable at all.
- **The rationale is auditable.** `bismuth log` shows `project=Apollo, year=2023`,
  not a paragraph of justification.
- **Refusal becomes expressible.** If any axis has no value, there is no path —
  `path_for` returns `None` and the document goes to the inbox. A partial answer
  never becomes a partial path.

Adjudication ([`prompts/placement.py`](../../src/bismuth/prompts/placement.py))
still exists for the cases arithmetic cannot reach: a null facet, a folder that
does not exist, two folders that both nearly claim the document. It is marked
`ADJUDICATED` in the placement record so a human can see which decisions were
opinions rather than computations. **The fraction of documents taking the derived
route is the best health metric Bismuth has** — when it falls, the taxonomy has
stopped describing what is arriving, usually well before anyone notices by looking.

## Consequences

- Consistency is structural rather than aspirational. It is not something the
  prompt asks for and the model tries to deliver.
- Two model calls per document instead of one (describe, then assign), because the
  two need different context: the description needs the document, the assignment
  needs the taxonomy's existing values. See [0004](0004-llm-provider-abstraction.md).
- **Extraction quality is now the whole ballgame.** Every placement error traces to
  a facet value. That is the point — the errors are localised and fixable — but it
  means the assignment prompt is the highest-leverage file in the repo.
- Anti-drift needs its own mechanism, since the model can still coin `Project
  Apollo` alongside `Apollo`. Handled by showing known values in the prompt and
  folding new ones back after each document (`Taxonomy.observe`). It is a
  mitigation, not a proof.
- The inbox fills up more than a guessing system's would. Working as intended: a
  loud inbox is recoverable, a quiet wrong folder is not.

## Revisit when

Never for the core path — this is the load-bearing decision of the project. But
adjudication's share is worth watching. If most documents route through it, the
taxonomy is not describing the corpus, and that is a finding about the *vault*
rather than an argument for letting the model choose folders.
