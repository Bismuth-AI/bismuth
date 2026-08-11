# Troubleshooting structured-output loops

## Symptom

A structured LLM call keeps streaming after it has already emitted the apparent
answer. The stream may repeat a short suffix until the model context is exhausted.
An inactivity timeout does not fire because chunks continue to arrive.

This was observed on 2026-08-11 with `qwen3.6-35b` behind an OpenAI-compatible
vLLM 0.24.0 endpoint. Placement was expected to return one folder handle but a
native JSON-schema request produced malformed JSON followed by repeated `]}` or
`<answer> F002</answer>` fragments.

## Evidence and reproduction

The original failing Placement request was replayed with the same system prompt,
user prompt, model settings, and native response schema.

- Input: 641 tokens
- Output: 64,895 tokens
- Total context: 65,536 tokens
- Stream chunks: 64,757
- Duration: about 358 seconds
- Finish reason: `length`
- Reasoning output: none

The replay reproduced the original failure. The retained run log contained six
Placement attempts above 64,000 output tokens. This makes the incident a
repeatable model/decoding interaction, not merely a slow request or a network
timeout.

Never paste an API key, gateway cookie, or complete private document into an issue
report. The application LLM log contains the full prompts and stream chunks and
must be handled as sensitive data.

## Controlled experiments

All Placement experiments used the exact failed prompt. The correct answer was
`F002`.

| Variant | Trials | Correct | Finding |
|---|---:|---:|---|
| Native JSON schema, `max_tokens=16`, temperature 0.7 | 5 | 0 | The cap stopped the runaway but truncated invalid JSON |
| No response schema, original prompt, `max_tokens=16` | 10 | 10 | Returned `F002` |
| Explicit allow-list, plain ID, temperature 0.7 | 10 | 10 | Returned `F002` |
| Explicit allow-list, plain ID, temperature 0 | 10 | 10 | Returned `F002` |
| vLLM `structured_outputs.choice`, temperature 0 | 10 | 10 | Correct, but provider-specific |
| Invalid capped primary followed by a clean plain-ID retry | 5 | 5 | Every retry recovered `F002` |

A client detector looking for an exact non-whitespace suffix repeated six times,
with pattern lengths from 2 through 40 characters, was replayed over all six stored
runaways. Five were detected after 39 output characters and one after 1,116. It
had zero detections over 72 normal experiment responses. These are regression
observations, not a proof that the heuristic can never false-positive.

Representative non-Placement schemas were also exercised repeatedly with bounded
generation:

- `CharterDraft`, 128 tokens: 5/5 valid
- `CardDraft`, 1,024 tokens: 3/3 valid in the experiment
- `BoundaryAudit`, 512 tokens: 3/3 valid
- Repair with only 2,000 characters of a synthetic 100,012-character invalid
  response: 3/3 valid
- Inactivity and absolute-deadline controllers: both passed deterministic stream
  tests
- Missing application-visible raw chunks in the 62-call integrity set: zero

Historical successful output sizes show why one global cap is unsafe:

| Schema family | Observed successful maximum | Initial safety cap |
|---|---:|---:|
| Placement plain choice | one handle; `F002` used 5 tokens in the experiment | 32 |
| Charter and small audits | 61 or fewer in sampled logs | 256 |
| Densified summary | 256 | 512 |
| Members / replacement sketch | 292 | 512 |
| Replacement | 443 | 768 |
| Replacement assignments | 590 | 1,024 |
| Card draft / update | 1,152 / 1,138 | 2,048 |
| Emerging-boundary analysis | 2,595 | 4,096 |

These caps are starting operational limits, not schema semantics. Revisit them with
real distribution data when prompts or models change. A `finish_reason=length`
must be logged distinctly so an undersized cap is visible.

## Diagnosis checklist

1. Find the failing call in `./logs/llm.jsonl` by document ID and call number.
2. Identify its schema, input/output tokens, duration, finish reason, stream chunk
   count, and maximum gap between chunks.
3. Distinguish inactivity from continuous output. A small maximum chunk gap plus
   a huge output count is a generation loop, not an idle timeout.
4. Inspect `reasoning_content` separately. This incident repeated in ordinary
   `content`; enabling or disabling hidden reasoning was not the immediate cause.
5. Replay only with an output cap or client abort guard. Do not reproduce an
   unbounded 65k-token stream casually.
6. Confirm whether the failure depends on native JSON-schema decoding by comparing
   the same prompt without `response_format`.
7. Preserve the complete raw application-visible stream in the diagnostic log.
   Bound only the fragment copied into a repair prompt; logging and repair context
   are different concerns.

## Safe operating rule

Placement uses the special plain-choice protocol recorded in ADR-0013. Other
structured calls keep schema validation but require a schema-specific output cap,
bounded repair context, repetition detection, an inactivity timeout measured from
the last received chunk, and a separate absolute generation deadline. Transport
retries remain disabled so an abandoned generation is not multiplied on the server.

If both Placement attempts fail, the upload remains in `_inbox`. Staging puts the
original there before model work and a successful validated placement is the only
operation allowed to move it out.
