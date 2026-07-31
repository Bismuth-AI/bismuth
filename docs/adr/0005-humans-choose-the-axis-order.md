# 0005 — The human chooses the axis order

**Status:** accepted

## Context

This is the decision the whole project is arranged around, so it is worth being
precise about what is being claimed.

Bismuth exists because organising 50GB of documents took two experienced people two
weeks, and afterwards an agent's retrieval accuracy jumped dramatically. The
structure was worth it; the two weeks were the problem.

The tempting reading is: *the two weeks were manual labour, so automate them.* That
reading is wrong, and building on it produces a tool that demos well and fails in
the field.

The two weeks were not spent moving files. They were spent **arguing about whether
project or year went on top**.

And that argument has no answer in the documents. Accounting wants years first.
A project manager wants projects first. The corpus is byte-for-byte identical
either way. The information that settles it lives in how *people* look for things —
which is not in the documents, is not in the metadata, and is not recoverable by
reading harder.

So a model asked to "organise these documents" will produce a tree. It will look
plausible. It will be defensible. And it will be wrong in the specific way that
nobody catches: no error, no symptom, just retrieval that quietly underperforms
six months later and a team that concludes AI search does not work.

## Decision

**Split the problem along the seam between what is in the documents and what is in
people's heads.**

| | who | how long |
|---|---|---|
| discover facets | the model, reading everything | minutes |
| **order them into axes** | **the human** | **seconds** |
| assign facet values per document | the model | ~2 calls |
| derive the path | pure arithmetic ([0003](0003-derived-paths-not-chosen-folders.md)) | instant |

[`FacetDiscoveryService.discover()`](../../src/bismuth/services/facets.py) returns a
taxonomy with facets and an **empty `axis_order`**. The empty field is the decision
made visible in a type: the function has deliberately not decided the one thing it
is not entitled to decide.

`bismuth discover` then shows the facets with their measured coverage, shows a
recommendation with reasons, and asks.

The recommendation is computed, not guessed:
[`recommend_axis_order()`](../../src/bismuth/services/facets.py) ranks by coverage
first (an outer axis a third of the corpus lacks makes a tree that is a third
"unknown"), then by kind — nominal facets discriminate best and match how people
ask; temporal is the tempting choice and usually the wrong one, because "the Apollo
contract" is a thing people look for and "a document from 2023" is not.

The CLI says out loud that it is guessing:

> *This is a guess from the documents. You know how people here look for things;
> Bismuth does not.*

## Consequences

- **Bismuth is not fully automatic, and says so.** The pitch is "two weeks becomes
  two hours", not "no humans". This is more honest and, we think, more sellable:
  people who have done this work do not believe the second claim.
- **Facets survive batching; trees do not.** This is also what makes discovery
  scale. Split 5,000 cards into batches and each batch confidently proposes a
  *different* tree, with no way to reconcile them — that is the wall naive versions
  hit long before 50GB. But "this slice has projects, years and document types"
  merges cleanly with the next slice's answer. Asking only for facets is what makes
  the map-reduce in `discover()` sound rather than a hopeful average.
- **Coverage becomes measurable.** `measure_coverage()` counts it from the corpus,
  which turns "which axis goes on top?" from a two-week argument into a table with
  numbers in it. That is most of where the two weeks actually goes.
- **Choosing wrong is cheap, eventually.** Secondary views (`by-year/` as links)
  mean one document is reachable along several axes without being copied. A human
  cannot maintain three trees by hand; a machine does it for free. That is the one
  place Bismuth is straightforwardly better than a person, and it is the reason the
  axis decision does not have to be perfect. *(Not yet implemented — see the
  Windows symlink problem in [0001](0001-filesystem-is-the-product.md).)*

**Costs.**

- A human must be present once, at the start. A fully headless deployment has to
  pass `axis_order` in configuration.
- The recommendation may be good enough that users accept it unread, which
  reintroduces exactly the failure this ADR is about — with our name on it. The
  wording of that prompt is doing real work and should not be softened for
  friendliness.

## Revisit when

Someone shows evidence that a model *can* infer axis order from a corpus alone —
measured against trees real teams built for themselves, not against a model's own
judgement of its own output. That would be a genuinely interesting result and would
obsolete this ADR. Until then, the honest answer is that we cannot see what people
ask for.
