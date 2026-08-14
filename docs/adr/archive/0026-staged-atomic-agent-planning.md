# 0026 — Stage planning and make family assignment atomic

## Status

Accepted.

## Context

Run `20260813T113321Z_fb1db47e87` showed that Qwen read `FAMILY=F001`, stated that
the three members must stay together, then submitted the law, decree, and enforcement rule
to different document-type shelves. Its next repair response spent the complete 4,096-token
application cap narrating variants of the same unresolved taxonomy and ended with
`finish_reason=length` and no tool call. Critic requests also retained old exploration tool
calls in their message history after the advertised schema had been narrowed to the
conclusion tool, so Qwen copied `tree`, `inventory`, and `sidecar` calls that were no longer
available.

## Decision

- A grounded multi-document family is one model-facing `F...` assignment unit. `D...`
  members remain evidence handles but are invalid assignment handles while their F unit is
  advertised. The host expands an F unit to its exact D members during validation.
- A pending window is expanded with only the exact grounded family mates it needs, including
  placed or deferred mates from earlier windows. Unrelated historical backlog is not replayed,
  and ordinary pending IDs yield their slots to keep the window at 30 documents.
- Vault root is not a reusable family destination.
- Planner and critics run read-only exploration first. A fresh transcript then receives
  flattened exact observations and only terminal tools, with required tool choice. Tool
  schemas never shrink midway through a transcript.
- Agent output defaults to 32K. A length stop escalates the same request to 64K, then permits
  at most three direct continuations. Context compaction and a high max-turn fuse remain
  separate safety mechanisms; low turn-triggered conclusion phases are removed.

## Consequences

Family cohesion no longer depends on a model remembering an instruction while constructing a
large membership object. Qwen's visible `content` may still contain deliberative prose even
with hidden thinking disabled, but that prose is confined to the evidence phase and cannot
replace the fresh required conclusion tool call. Debug logs identify `.explore` and
`.conclusion` agent stages independently.
