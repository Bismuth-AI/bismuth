# 0016 — Signs that discriminate, names that fit, and a boundary torn down only on agreement

**Status:** accepted
**Amends:** [0011](0011-bounded-maintenance-and-stable-signs.md), [0014](0014-boundary-state-and-closed-membership.md)

## Context

A 300-document run finished cleanly and produced a bad tree. The mechanics were not the
problem: 5,353 calls, two retries, zero JSON parse failures, zero truncations, zero
repetition, zero transport errors, and a largest request of 29,596 characters against a
32,000 budget. Everything that failed, failed above the transport.

Four causes, all visible in the run's own artifacts.

**A field budget is read as an instruction.** `ReplacementSign.name` carried
`max_length=120`. The median name it produced was exactly 120 characters, and 83% were
longer than the 64-character path segment they had to become, so `sanitize_segment` cut
them into truncated sentences that are now folder names. In the same run, by the same
model, `Emerging.name` — which has no ceiling and one sentence of description — had a
median length of 10 characters and exceeded the limit once.

**A derived sign carries no information.** [0014](0014-boundary-state-and-closed-membership.md)
replaced model-written folder notes with `<axis>: <class name>` because the model had
written request-local `D####` handles, exclusions and its own decision process into a
public file. The projection cannot leak, and it also cannot discriminate: the axis is
identical across every sibling, so the sign restates the folder name. Review was shown
those signs and answered "the current signs do not help a reader rule alternatives out"
in 25 of 26 packets. It was right.

**Unanimity to hold made destruction the default.** A review splits into as many as six
evidence packets, each judging the whole boundary from its own slice, and the checks were
combined with `all`. Every one of 13 reviews failed. The replacements then discarded
working names — `전자상거래 및 통신판매`, `가맹사업 및 전통시장`, `금융·보험 및 신용정보` —
for the truncated sentences above.

**Equality is the wrong test for a repeated distinction.** The spent-axis and
ancestor-name guards fired 19 and 1 times, and still allowed
`대통령령 총리령(하위시행규정)/…/대통령령`: the grandchild names one half of a compound its
ancestor had already resolved, and normalised equality does not see it.

## Decision

**A name must fit the path it becomes.** `validate_plan` rejects a class name longer than
the path segment limit rather than letting sanitisation cut it. A name that has to be cut
was never a sign, and cutting silently is what hid that a sentence had been proposed.

**No character ceilings on name or axis fields.** Generation stays bounded by the
schema's output cap, which is a transport circuit breaker; it never fired in the run that
produced the saturated names. A ceiling on a semantic field is a budget the model spends.

**A child's sign says what belongs there that does not belong behind a sibling.** The
model writes it, in one dedicated field, with the siblings visible. The application never
trusts it: a sign carrying a request-local handle, spanning lines, or running past the
sign budget falls back to the derived `<axis>: <class name>`. A fallback sign is worse
than a good one; a wrong one is a lie on disk and a rejected one loses a document that is
already safe, so degrading is the only acceptable failure. The handle pattern is not
anchored on word boundaries — in a language that does not space its particles, `D0001과`
has none, and that is the shape the observed leak took.

Nothing re-derives a sign that is already usable. Three code paths did: the migration to
stable notes, the boundary groups shown to review, and the subtree listing. Review judged
boundaries by signs no reader would ever see.

**A boundary is replaced only when a majority of the evidence agrees.** Packet checks are
combined by majority, not by `all`. Fail-closed belongs on mutation; a review failure is
not a no-op but the trigger for the most destructive operation in the system, so the
conservative answer to "tear this down" is no. Each packet's own verdict is logged, so a
merged answer can always be traced to the packets behind it.

**A descendant name inside an ancestor's is a repeat.** Containment, one direction only:
a name wholly inside an ancestor's says nothing new at a depth where that distinction is
already fixed to one value. The other direction is ordinary refinement and is allowed.
The same containment applies to axes.

Naming shapes that are neither of these — an `A vs B` name that offers a choice instead
of answering one — stay with the semantic audit. Detecting them in code would need a
vocabulary, and the domain does not guess meaning.

## Consequences

- A sentence can no longer become a folder, in either the additive or replacement path.
- Folder notes can discriminate again, which is what [SPEC.md 3.6](../../SPEC.md) asks of
  them and what an agent browsing the tree actually reads.
- A model that writes a bad sign costs information, never a document.
- Boundaries survive a single dissenting packet, and a genuinely broken one still fails.
- Replacement gets rarer, so the tree changes shape less often between arrivals.
- The tests for closed-choice membership, routing and assignment now exercise the ADR-0014
  contract rather than the JSON schemas it replaced; the suite had been red since.

## Revisit when

Structure is measured again on a full corpus, in more than one input order. This record
fixes causes observed in one run against one model and one corpus; that the causes were
real is evidence, that the fixes are sufficient is not yet measured.
