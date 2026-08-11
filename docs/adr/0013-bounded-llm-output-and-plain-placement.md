# 0013 — Use a plain choice for placement; bound every LLM generation

**Status:** accepted
**Amends:** [0004](0004-llm-provider-abstraction.md)

## Context

Placement asks a small closed-set question: choose one offered direct-child handle,
stay at the current folder, or declare the document unreadable. It does not need an
open-ended JSON object. Nevertheless it used the same native JSON-schema path as
large multi-field catalogue and maintenance responses.

With `qwen3.6-35b` and a vLLM OpenAI-compatible endpoint, the combination sometimes
emitted the correct-looking folder ID inside malformed JSON and then repeated short
suffixes until the 65,536-token context was full. An inactivity timeout could not
help because the server continuously delivered chunks. Replaying the exact failed
prompt reproduced the behavior.

The `confidence` field in the Placement response did not protect filing. Low values
were intentionally still accepted, so it was model-authored diagnostic metadata,
not a safety signal. Keeping JSON only for that field increased failure surface
without improving the decision.

The experiment and operational evidence are recorded in
[the troubleshooting guide](../troubleshooting/structured-output-loops.md).

## Decision

Placement has a dedicated provider-neutral choice protocol:

1. Offer opaque, short, request-local handles such as `F001` and `F002`, plus the
   literal choices `STAY` and `UNREADABLE`.
2. Request exactly one allowed token, without native JSON schema or JSON mode.
3. Use temperature 0 and a 32-token generation cap. The tested model needed five
   output tokens for `F002`; 32 leaves room for different tokenizers and end-of-turn
   tokens without giving malformed prose meaningful room to grow.
4. Strip surrounding whitespace, normalise ASCII case, and require exact allow-list
   membership. Do not fuzzy-match, recover embedded handles, or accept prose.
5. On invalid output, retry once with a short clean prompt. Do not include the bad
   reply or a validation conversation in that retry.
6. If the retry is invalid or unavailable, leave the original in `_inbox` and report
   the placement failure. Only a validated choice may move the file.

The default remains provider-neutral plain text. A backend-native choice constraint,
such as vLLM `structured_outputs.choice`, may be an explicitly detected optimisation,
but correctness must not depend on it.

The general structured-output adapter keeps JSON schemas for tasks that genuinely
need multiple fields. Every such call has all of these independent guards:

- a reviewed per-schema output-token cap;
- full application-visible prompt, chunk, partial-output, timing, finish-reason, and
  error logging, without truncating the persisted diagnostic record;
- a bounded invalid-output fragment in repair prompts;
- conservative client-side repetition detection;
- an inactivity timeout reset by every received chunk;
- a separate absolute generation deadline that continuous output cannot reset;
- zero automatic transport retries.

The full raw response is never copied wholesale into model context merely because it
was preserved in a log. Diagnostic retention and recovery context have different
trust and size boundaries.

## Consequences

- Placement can no longer fail because a one-value decision was wrapped in malformed
  JSON.
- Placement no longer records model self-reported confidence. Observability records
  the offered set, selected literal, retry/guard outcome, and timings instead.
- `max_tokens` prevents unbounded cost but is not treated as a correctness mechanism;
  output must still validate.
- General structured tasks retain expressive schemas and repair, with limits sized
  from their own observed distributions rather than one global number.
- Client repetition detection is defence in depth. It may require tuning as new
  legitimate output shapes appear, so every abort preserves the complete partial
  stream and detected pattern for diagnosis.
- An absolute deadline can terminate a healthy but unusually slow generation. Its
  value must therefore be configurable and measured from actual provider execution,
  not silently conflated with queue wait or inactivity.

## Revisit when

- a provider-neutral API offers constrained enum decoding consistently across the
  supported backends;
- evidence shows the plain choice reduces placement accuracy compared with another
  bounded protocol;
- observed legitimate responses regularly approach a schema cap; or
- repetition detection produces a confirmed false positive.
