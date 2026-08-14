# 0031 — Establish first boundaries with sibling contrast

## Status

Accepted. Amends ADR-0030.

## Context

Run `20260814T004719Z_29decc9289` completed 105 documents without file failures but
created a mixed root. Four early documents produced one provisional sign,
`산업 진흥법 시행령`, and a packet-local comparison was stored as the permanent root
question. Later calls received only the short axis label `법령의 성격`, reinterpreted it
as topic, and added `금융`, `소비자 보호 및 시장 질서`, and other non-sibling signs.
The same model's affirmative boolean audit did not falsify its proposal. Placement also
made 705 evidence calls; 25 documents reached 16 turns without a terminal decision.

## Decision

- An undivided parent establishes an axis only with at least two evidenced sibling signs
  proposed together. Each sign still needs at least two members; unclaimed documents stay
  at the parent.
- The stored axis question is immutable input to every later sibling proposal and reduce
  call. The model may not regenerate or reinterpret it.
- Boundary review is adversarial: it returns concrete blocking violations in addition to
  checks, and any violation rejects the proposal.
- Placement is a finite evidence protocol: decide directly, or inspect one addressable
  item and conclude. Repeated traversal is not an agent capability.
- Filename-grounded title/topic identities bind acts, subordinate instruments and editions.
  A family at root remains together, and subdivision moves colocated family members as one.

## Consequences

The first folder appears later than a one-sign design, but it no longer lets one early
cluster define a meaningless global axis. Local models make shorter placement transcripts,
and failures fall back before exhaustive vault traversal. Family cohesion is enforced by
the application rather than delegated to a prompt.
