# 0023 — Keep retry windows family-cohesive and review exact plans

## Status

Superseded in part by ADR-0024. Family-cohesive packing remains accepted; automatic
deferred replay and whole-vault critic visibility do not.

## Context

An 86-document Qwen run was correctly isolated into windows of 30, 30, and 26, yet
all three windows moved zero documents. Run `20260813T062314Z_0fb75044a1` supplied
the decisive evidence.

The first candidate was silently rewritten by deterministic family closure before
semantic review. The planner placed a decree under `대통령령`, but the critic received
it under `법률`; subsequent correct revisions therefore looked stale. Failed documents
then entered `deferred_document_ids`. Later planners could address only new pending IDs,
while whole-vault validation still rejected plans that separated those IDs from hidden
deferred family members. No candidate could satisfy both constraints. One Qwen turn spent
79.9 seconds and the full 16,384-token ceiling reasoning around this impossible state.

## Decision

- A grounded document family is indivisible when packing a maintenance window.
- A new upload consumes only its pending IDs. Deferred IDs from an earlier failed decision
  do not displace new arrivals or silently enter a different evidence window.
- One maintenance drain selects a document at most once. A failed candidate cannot create
  an immediate retry loop.
- Successfully filed deferred IDs leave the checkpoint. Only documents still loose at
  the reviewed scope remain deferred.
- Deterministic validation never changes submitted targets or membership. It may reject
  a family split, but the semantic critics receive the exact validated candidate.
- Agent chat output is capped at 4,096 tokens. A successfully accepted terminal tool
  ends the agent loop without an extra model turn.

## Consequences

The model and critics share one inspectable plan identity, and a later bounded window has
the authority needed to repair an earlier unresolved family. The 30-document isolation
remains real; this is not a global retry. Large repetitive prose is bounded, while normal
30-document tool-call JSON still has sufficient output space.
