# 0012 — Use one configured model for every task

**Status:** accepted

## Context

Bismuth originally exposed `FAST` and `REASONING` profiles so users could trade
cost against capability. In practice this doubled setup choices, made every service
carry routing metadata, and implied a behavioural distinction that did not necessarily
exist. A profile selected a model and optionally forwarded `reasoning_effort`; it did
not itself enable or disable model thinking. Local users commonly selected the same
model twice.

## Decision

The user selects one model. Cataloguing, placement, folder signs, and maintenance all
use it through the same structured LLM port. Provider-specific behaviour such as
Qwen's `chat_template_kwargs.enable_thinking` remains an explicit request-body setting
and therefore applies consistently.

Old config files are read by preferring their former judgement model, then their fast
model. The next settings save writes only `model`.

## Consequences

Setup has one choice and task code no longer knows about model tiers. Operators who
want different performance characteristics must choose a generally suitable model or
tune the endpoint globally. If real deployments later demonstrate that multiple models
materially improve quality or cost, that should return as an evidence-backed scheduler,
not as two unexplained setup fields.
