# 0032 — Retry declined first boundaries only after evidence growth

## Status

Accepted. Amends ADR-0031 and the retry schedule described in the subdivision spec.

## Context

Run `20260814T013505Z_fc0d3aa17a` filed 150 documents successfully, but took 41 minutes
51 seconds and made 2,757 LLM calls. Initial-boundary sketches made 729 calls and their
membership checks made 1,385 calls. Most were near-identical retries in undivided leaves
after a candidate had already been rejected.

The same run exposed two independent correctness defects. Family closure silently added
omitted relatives to a proposed sign, so an Act could be dragged into a subordinate-rule
shelf. Multipart clients using RFC 2047 encoded Unicode filenames also reached Windows as
literal encoded words and failed before staging.

## Decision

- The first feasible initial-boundary judgement still happens as soon as four direct
  documents can satisfy two sibling signs with two members each.
- After that judgement is declined or rejected, the live process retries only when the
  loose evidence has grown by at least four documents and approximately fifty percent.
  This is request deduplication, not a document-count rule for whether a folder deserves
  subdivision. The model continues to decide the semantic boundary.
- The retry memory is process-local. It neither becomes taxonomy state nor survives a
  restart.
- Family validation checks the model's exact proposed destinations, including the parent
  as a destination. A partial or split family proposal is rejected; code never rewrites
  it by silently adding members.
- Boundary audit violations are bounded codes, and filesystem sign names are short labels,
  not definitions or bilingual explanations.
- Upload adapters decode standard RFC 2047 filenames before basename/path validation.

## Consequences

An undivided leaf can remain flat for a few more arrivals after a rejected proposal, but
the system no longer spends hundreds of calls asking the same question. Family cohesion
cannot falsify the semantic meaning of an accepted shelf. The next real-corpus cycle must
measure call count, elapsed time, folder-name quality, and family placement again; unit
tests alone do not establish those outcomes.
