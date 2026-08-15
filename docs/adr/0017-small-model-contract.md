# 0017 — What a small model is asked, and what it is never asked to remember

**Status:** accepted
**Amends:** [0013](0013-bounded-llm-output-and-plain-placement.md), [0014](0014-boundary-state-and-closed-membership.md), [0016](0016-signs-that-discriminate-and-names-that-fit.md)

## Context

[SPEC.md §2.1](../../SPEC.md) now names the model class Bismuth targets. This record is
what a tuning round against `qwen3.6-35b` measured, on 24 and 30 documents at a time, with
each round in a fresh vault.

The two failure modes people expect from a small model did not appear. Across roughly
2,000 calls there were seven retries, no truncation, no repetition loop, no JSON that
failed to parse, and no context overflow. Structured output was not the problem.

What did go wrong, every time, was a question that carried more than one judgement, or a
contract the model was never told.

**A field description is not an instruction.** Under native constrained decoding the
grammar enforces structure; the descriptions are not reliably part of what the model
conditions on. A `sign` field added with a careful description came back equal to the
folder name until the same contract was written into the prompt body. The two audit checks
added in [0016](0016-signs-that-discriminate-and-names-that-fit.md) answered `true` for a
boundary they exist to reject, for the same reason.

**A field budget is an instruction.** `max_length=120` on a name produced a median name
length of exactly 120 ([0016](0016-signs-that-discriminate-and-names-that-fit.md)).

**Three booleans in one reply is three judgements.** Review returned all-false on
boundaries whose signs were specific and correct, and a false review is what triggers a
complete replacement — so the least reliable answer in the system drove the most
destructive operation.

**The instruction's language is the answer's language.** With the prompt in English and
the last Korean token removed from it, one round produced twelve proposals named in
English over a Korean archive. All twelve were refused by the script guard, and the tree
starved: 40% of documents never left the root.

**Sampling is part of the question.** The operator's configuration ran every
classification call at `temperature: 0.7` with `presence_penalty: 1.5`. A presence penalty
discourages reusing tokens already in the context, and placement's whole purpose is to
give back one of the folder names it was just shown.

## Decision

One judgement per call. Review became one closed `HOLDS`/`FAILS` question per check
against the same evidence, joining membership and routing, which
[0014](0014-boundary-state-and-closed-membership.md) had already closed.

Every contract appears in the prompt body. A field description may repeat it; it may not
be the only place it exists. This applies to audit checks as much as to generated values.

No character ceilings on semantic fields. Generation stays bounded by the schema's output
cap, which is a transport circuit breaker.

Bismuth owns sampling for schema-bound and closed-choice calls. Whatever the endpoint
itself requires still passes through, and the interactive agent path keeps the operator's
values.

The collection's language is read off the cards and named back to the model at the end of
the request. Nothing in the code names a language.

Proposals are compared against the evidence in the same request. An axis, name, or sign
that quotes a document it is sorting is refused — a title is a value of nothing, and one
replacement recorded four law titles as a folder's property. The comparison needs no
vocabulary, so it means the same thing for any collection.

## Consequences

- Review costs three small calls per packet instead of one larger call. Review is rare.
- A model that answers in the wrong language is corrected by evidence rather than by a
  builtin, so an English archive still gets English signs.
- Prompts carry more text. That is the cost of the contract being where the model reads it.
- The operator loses sampling control over classification calls and keeps it for chat.

## What splitting does not fix

Review split cleanly into closed per-check calls. The boundary audit did not, measured
three times: as questions, three checks failed nearly every proposal and thirty documents
stayed flat; as statements ending on the failure condition, five of six failed and the
archive stayed flat again; ending on the holding condition, one folder formed out of
thirty documents. It was reverted to a single call.

The difference is what the checks are about. Review judges a boundary that exists, and
every check has something in front of it. The audit usually judges ONE proposed class,
and most of its checks ask about siblings that are not there — asked in isolation, "do
these names overlap" has no honest answer for a single name, and the model resolves the
ambiguity by refusing. A checklist written for a complete boundary cannot be taken apart
and asked item by item about a single shelf.

The recency effect is separate and real: whichever condition a closed prompt named last
was the answer that came back. That is the same effect this repository already handles by
generating a verdict field after the evidence, and it should be assumed for every closed
question.

## The axis, which took eighteen rounds

The property a folder is divided on outlives every other judgement: every later question
about that folder is asked against it. It was also the judgement made with the least
evidence -- fixed the moment one class came out, from as few as five documents -- and
nothing short of a destructive replacement could revise it. Both rounds that chose a
format axis fixed it at five documents; every subject axis was fixed at fifteen or more.

Three things fixed it, and the order matters because the first two alone did not.

1. **The contract reached the model.** The rule that an always-present attribute is not
   the first cut existed only in a field description, and field descriptions do not
   survive constrained decoding.
2. **The axis is provisional while the boundary is.** ADR-0014 already called a folder
   with one child provisional; the axis was not. It is now re-asked, with the existing
   child shown, until two children make a boundary. Nothing moves -- the child keeps its
   name and its documents, and only the recorded property changes.
3. **The property is checked on its own.** Inside the six-boolean audit this check
   approved 문서의 성격, 주관 부처 and 법령의 성격 -- every axis it exists to reject. As a
   single closed question it refuses them. Splitting *all six* was measured three times
   and was worse each time; this one splits cleanly because it is about the property
   alone, which is present whether one class is proposed or twelve.

## Revisit when

The measured numbers move. Thirty documents, both input orders, at the end of eighteen
rounds:

| | first measured | now |
|---|---|---|
| same-folder pair F1 between orders | 0.39 | **0.68** |
| documents left at the root | 40% | **3% / 0%** |
| root axis on subject rather than format | forward only | **both orders** |
| leaf sizes | 2-24 | 3-12 |
| destructive replacements | 3 | 0-1 |
| retries per ~360 calls | -- | 1-2 |

F1 will not reach 1.0 and should not be read as if it could: two different reasonable
organisations of the same corpus score low against each other by construction. What
matters is that both are now organised on what the documents are about, and that a
document lands somewhere in both.

Still open: leaf sizes reach twelve against a browsing target nobody has set, a parent
sometimes holds one document and one child, and none of this has been measured beyond
thirty documents or outside one corpus, one language and one model.
