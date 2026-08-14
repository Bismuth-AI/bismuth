# Qwen repetition incidents — raw-trace reconstruction

This note records the two repetition shapes observed in run
`20260813T144820Z_80abd179cb`.  It is a regression reference, not a general claim that every
long response is repetition.

## 1. Planner exploration narrated instead of terminating

- Stage: `planner.explore`, window 3
- Call: `llm_fe4e920beb4345cf99b04190ce1dac13`
- Actual input: system required one `tree`, one `arrivals`, minimal ambiguous reads, then
  `finish_exploration`; transcript already contained the 29 arrival cards, root note, and
  four family reads; `finish_exploration` was present in the tool schema.
- Actual output: no tool call; 18,847 content characters / 8,038 output tokens repeatedly
  recounted the 29 documents and cycled through “Wait”, “Actually”, “Let me reconsider”, and
  “Let me finalize”.
- Stop: `repetition_guard`, pattern `i don't need to actually create the`.

The causal execution state was “evidence sufficient, but free-form prose still legal and
finish not terminal”.  Prevention is therefore required tool choice for explorer turns,
an inspect-only user instruction, and immediate termination on the first accepted finish
result.  Raising presence penalties is not the contract fix.

## 2. Native CardDraft repeated inside one legal JSON string

- Operation/schema: `structured` / `CardDraft`
- Call: `llm_61bbf10ef7934c68a46dfb0b982ec30f`
- Actual request: `native_schema=true`, `response_format=json_schema:CardDraft`, input 5,787
  tokens, output cap 2,048.
- Actual output: a correctly shaped JSON prefix whose sixth `answers_questions` item kept
  appending the same group of Korean questions.  The item count stayed within six, so the
  old schema did not prohibit the content loop.
- Stop: `finish_reason=length`; JSON was incomplete.  A clean native retry stopped normally
  at 804 tokens and validated.

The causal schema state was “bounded array, unbounded item string”.  Prevention keeps native
structured output and adds item-level maximum lengths.  The structured stream also detects
long recurring word sequences and logs the exact pattern/character offset if a backend does
not enforce `maxLength`.

## Debugging checklist

For a future repetition report, verify all of the following before changing prompts or
sampling values:

1. Exact run/window/stage/call ID.
2. Request messages, tool schema, `tool_choice`, native schema flag, and reserved output.
3. Whether evidence was already sufficient when repetition began.
4. Whether repetition occurred in prose, reasoning content, tool arguments, JSON structure,
   or one JSON string.
5. Raw finish reason or stream abort pattern and character offset.
6. Whether the relevant terminal/schema constraint was actually advertised and enforced.

