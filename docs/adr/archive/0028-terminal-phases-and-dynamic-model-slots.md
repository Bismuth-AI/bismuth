# 0028 — Make agent phases terminal and reserve model slots dynamically

## Status

Accepted.

## Context

Run `20260813T144820Z_80abd179cb` exposed two different repetition failures and several
execution-contract faults.  Planner exploration call
`llm_fe4e920beb4345cf99b04190ce1dac13` had already received `tree`, all 29 arrivals, the
root note, and four family reads.  Instead of calling `finish_exploration`, it repeatedly
recounted the same documents for 18,847 characters until the prose repetition guard stopped
it.  Structured CardDraft call `llm_61bbf10ef7934c68a46dfb0b982ec30f` used native JSON
Schema, but one unbounded `answers_questions` string repeated until the 2,048-token output
cap truncated the JSON.

The same run requested 32,000 output tokens for inputs that left only 31,999 tokens in a
65,536-token endpoint context.  Semantic-review transport failure then poisoned the exact
candidate fingerprint.  Existing routing targets were presented to the critic with the
same label as new targets, and successful `finish_exploration` calls were not terminal.

## Decision

- Agent output is a per-call reservation: estimate the exact messages plus tool schemas,
  retain a tokenizer/ChatML safety margin, and reserve at most the remaining context.  The
  32K and 64K values remain economical ceilings, never unconditional requests.
- Explorer calls advertise `tool_choice=required`.  A successful `finish_exploration` is a
  hard phase boundary and returns without another model turn.  Planner exploration receives
  an inspect-only user task rather than the conclusion instruction to propose a structure.
- A semantic reviewer exception is an unavailable review, not a rejection.  It consumes no
  reviewed-candidate slot and does not retain the candidate fingerprint.
- Candidate evidence marks every move target as `existing_target` or `new_target`.
  `duplicate_boundary` cannot block merely because an existing target is used for routing.
- Native CardDraft JSON Schema remains in use.  Every generated string has a semantic-sized
  `maxLength`; array cardinality alone is insufficient.  Structured streams also record
  long recurring word sequences as diagnostic circuit-breaker events.
- Model-facing read paths accept one display-style leading slash and normalize it to the
  vault-relative port contract.
- Status polling retries a transient Windows inbox read lock briefly and records a deferred
  read instead of returning HTTP 500.
- FastAPI reopens the CLI-created diagnostic run after uvicorn logging reconfiguration.  It
  does not create a second empty run.  The server manifest retains process status and also
  records active/last batch completion explicitly.

## Consequences

The primary prevention mechanism is a smaller state space, not sampling or output cleanup:
an explorer cannot narrate a free-form final answer when the provider honors required tool
choice, a finish tool cannot fall through to another turn, and one CardDraft string cannot
grow without bound while remaining schema-valid.  Repetition guards remain diagnostic
circuit breakers for provider/model violations, not the normal completion mechanism.

