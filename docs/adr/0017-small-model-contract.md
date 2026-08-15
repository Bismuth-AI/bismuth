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

## Revisit when

The measured numbers move. At the time of writing, on 30 documents in both input orders:
same-folder pair F1 between orders is 0.60, root residue swings between 0% and 47% run to
run, and leaf sizes sit between 3 and 12. The axis is chosen on subject rather than on
format in both orders, which it was not before. None of that is settled; it is the first
baseline that was measured at all.
