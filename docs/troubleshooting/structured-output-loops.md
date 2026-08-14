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

### Follow-up: output-cardinality failures

The first guarded 300-document run exposed a separate failure on the same day. It
was not a generation loop: 21 post-filing maintenance calls ended normally with
`finish_reason=length`, but their JSON objects were incomplete.

| Schema | Failed calls | Old cap | Why it grew |
|---|---:|---:|---|
| `ExistingAssignments` | 16 | 256 | The reply echoed many 16-character catalog hashes |
| `ReplacementSketch` | 3 | 512 | The model wrote several signs and inventory-like notes |
| `Replacement` | 2 | 768 | A complete plan echoed every document membership |

All three repair attempts used the same cap and therefore ended at almost the same
character every time. More schema retries could not help. The error text suggesting
a larger retry count or model was misleading.

The correction treats input context and output cardinality as different budgets:

- catalog hashes remain internal; every maintenance view exposes only deterministic,
  request-local `D####` handles;
- schemas that return document handles receive at most 12 documents per call and the
  application merges packets;
- replacement always uses membership-free sketches followed by bounded assignment
  packets, even when the whole input would fit in one context;
- replacement signs have structural count and string-length limits, so a note cannot
  become an inventory;
- `finish_reason=length` triggers a clean retry with a larger schema ceiling, not a
  malformed-JSON repair using the truncated reply;
- if an API body explicitly sets a lower maximum, the application reports the cutoff
  immediately because retrying cannot override that user limit.

The same run also produced a native-schema `Emerging` reply that began valid JSON and
then streamed whitespace. The client now aborts 512 consecutive whitespace characters,
recognises LiteLLM's wrapped `repeating the same chunk` exception, and retries the
original task without partial output or native constrained decoding.

## Diagnosis checklist

1. Use `scripts/inspect_run.py logs` to identify the failing `call_id` by run,
   maintenance window, stage, and document ID. `./logs/llm.jsonl` is only a compact
   current-run index.
2. Run `scripts/inspect_run.py logs --call <call_id>` and inspect the exact request
   and reconstructed response artifacts. Identify its schema, effective generation
   parameters, input/output tokens, duration, finish reason, and stream chunk count.
3. Distinguish inactivity from continuous output. A small maximum chunk gap plus
   a huge output count is a generation loop, not an idle timeout.
4. Inspect `reasoning_content` separately. This incident repeated in ordinary
   `content`; enabling or disabling hidden reasoning was not the immediate cause.
5. Replay only with an output cap or client abort guard. Do not reproduce an
   unbounded 65k-token stream casually.
6. Confirm whether the failure depends on native JSON-schema decoding by comparing
   the same prompt without `response_format`.
7. Inspect `streams/<call_id>.jsonl.gz` only when chunk timing or provider-level
   repetition matters. The complete application-visible stream is preserved there;
   bound only the fragment copied into a repair prompt because logging and repair
   context are different concerns.

## Safe operating rule

Placement uses the special plain-choice protocol recorded in ADR-0013. Other
structured calls keep schema validation but require a schema-specific output cap,
bounded repair context, repetition detection, an inactivity timeout measured from
the last received chunk, and a separate absolute generation deadline. Transport
retries remain disabled so an abandoned generation is not multiplied on the server.

If both Placement attempts fail, the upload remains in `_inbox`. Staging puts the
original there before model work and a successful validated placement is the only
operation allowed to move it out.

### Follow-up: limits fixed transport but regressed classification

A later guarded run proved that applying the same idea to maintenance membership was
the wrong abstraction. The root created one child containing four documents while 44
remained loose. That provisional one-child tree was reviewed as a complete boundary;
replacement repeatedly failed validation, and each failure returned before existing
routing or another class could emerge. Calls per document rose without structural
change. The child note also contained `D####` handles and exclusion reasoning.

The correction is architectural (ADR-0014), not a larger output cap:

- managed child notes are deterministically derived from axis and class name;
- membership uses one closed plain choice per document, never JSON ID arrays;
- one-child boundaries are provisional and cannot trigger complete review;
- failed repair attempts are persisted and normal additive filing continues;
- the same unchanged repair is not retried until evidence materially grows.

Generation limits remain enabled only as circuit breakers for malformed or runaway
provider output. Passing under a character or token limit is never classification
evidence.
