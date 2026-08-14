# 0020 — Repair sibling misroutes and keep blocking findings sticky

**Status:** accepted
**Amends:** [0019](0019-exact-candidate-semantic-review.md)

## Context

A real 124-document Qwen run completed every ingest and maintenance window but exposed
three independent gaps in the exact-candidate review design.

First, the root architect correctly identified documents already filed below the wrong
top-level sibling. `route_existing` could move only documents loose at the parent, while
`replace_boundary` required enumerating every document below every old child. The least
powerful available operation was therefore too weak and the next operation was far more
powerful than the repair required. Known misplacements remained in the finished tree.

Second, the host described two candidates as a limit but only instructed the model to
stop. A model could submit a third or fourth candidate and the handler would still review
and accept it. The same run also showed a blocking finding disappear after a revision
that left every cited document in the same destination.

Third, the critic tool required a free-form summary no longer used for the decision and
placed short list limits on evidence. Qwen repeatedly failed those incidental constraints,
then sometimes submitted an empty finding list after shortening its answer. Model prose
also became the maintenance completion summary, allowing its counts and claims to disagree
with the applied transaction.

## Decision

Add `rehome_existing` as a separate least-powerful structural right. It moves a document
already somewhere below one existing direct child to another existing direct child of the
assigned parent. It never creates or rewrites a boundary sign. It is rejected when the
target is not an existing direct child, the source is not below a sibling, either side is
human-managed, or the move would empty an existing boundary value. A repair that changes
the sibling set remains a complete `replace_boundary` operation.

The host, not the prompt, enforces at most two actually reviewed semantic candidates.
Malformed, mechanically invalid, repeated, and unchanged-sticky submissions have a separate
validation-failure ceiling, so a small tool-argument error or stale revision does not consume
the only semantic revision. Repeating an exact rejected candidate is rejected without
another critic run.

Each blocking finding records the destinations of its cited `D` handles in the rejected
candidate. A revision that leaves all cited placements unchanged is rejected before a new
critic sample. Findings without usable handles retain the complete boundary signature.
This does not decide semantic correctness; it only requires the model to actually change
the state that its previous critic declared blocking.

`submit_review` contains structured findings only. Free-form reviewer summary and arbitrary
evidence-list length limits are removed; generation budgets remain the transport guard.
After application, the host renders moved counts, operations, targets, and unresolved focus
counts from the validated plan and filesystem result. Model narration is not a completion
record.

No filename suffix, document family rule, language vocabulary, or domain taxonomy is added.
Related-card signals remain retrieval hints interpreted by the isolated critic, as required
by the corpus-neutral specification.

## Consequences

- An architect can correct a known existing sibling misroute without restating a large
  established boundary.
- A model cannot bypass the one-revision contract by ignoring tool text.
- A cited blocking defect cannot disappear solely because the same model sampled a more
  permissive verdict on unchanged placements.
- Reviewer schema failures no longer arise from unused prose length or evidence count.
- Maintenance summaries are replayable applied facts rather than model self-report.
- A bad semantic finding can still force an unnecessary revision. The second isolated
  review remains responsible for judging the changed candidate; the host does not infer a
  domain answer.

## Revisit when

- repair traces show that moving into the direct target should preserve a deeper compatible
  subpath rather than placing the document at that target root;
- concrete findings routinely lack usable handles; or
- measured reviewer quality justifies a separately configured critic model despite the
  single-model product contract.
