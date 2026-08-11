# 0004 — Route by profile, not by model name

**Status:** superseded by [0012](0012-one-model.md)

## Context

Anyone with documents worth organising asks the same first question: *do the
documents leave the building?* For a law firm, a hospital, or a defence contractor
the answer has to be **no**, and it has to be no without a fork.

"Supports local models" is easy to claim and usually false in practice. It fails in
two places:

1. **Wiring.** If services name models, local operation is a code change. It rots
   the moment nobody is testing it.
2. **Capability.** Even correctly wired, an 8B model cannot do what a frontier
   model does. Asked to "extract the concepts and relationships you find", it
   returns prose, then JSON with invented keys, then a different vocabulary on the
   next document. The tool technically runs locally and produces garbage — which is
   worse than not supporting it, because the failure is deniable.

## Decision

### Services request a profile, never a model

Nothing in Bismuth names `claude-sonnet-5` or `qwen3:8b`. A service says what a
call is *worth*:

| profile | when | examples |
|---|---|---|
| `FAST` | once per document or more | summarising, extracting a facet value |
| `REASONING` | once per corpus, or a hard case | facet discovery, adjudication, drafting a proposal a human will be asked to approve |

[`config.py`](../../src/bismuth/config.py) maps profiles to models. Point both at
one local model and Bismuth runs offline with no code path changed. `bismuth
doctor` reports `Data leaves this machine: no — fully local`, computed from the
config rather than promised by a README.

[LiteLLM](https://github.com/BerriAI/litellm) (MIT) does the dispatch, which turns
"support every provider" from a maintenance burden into a config string.

### Every interaction is schema-constrained

There is no `chat()` on the [LLM port](../../src/bismuth/ports/llm.py). Every call
declares a Pydantic schema and gets a validated instance or raises.

This is the concession that makes small models viable. The same 8B model that
improvises on an open-ended request is reliable when asked *"which of these seven
values is this document's project?"* against an enum. Constraining the interface is
what forced the pipeline to decompose into tasks small models can actually do — the
two-call split in [`services/cards.py`](../../src/bismuth/services/cards.py) is a
direct consequence.

### Degrade through three tiers of structured output

Support in the wild is not uniform, and a tool that only works on tier 1 does not
really support local models:

1. **Native schema enforcement** — the provider constrains decoding. Malformed
   output is impossible.
2. **JSON mode** — syntactically valid JSON, arbitrary shape. Keys get invented.
3. **Nothing** — you ask in the prompt and hope. Prose happens. Markdown fences
   happen. "Sure! Here's the JSON:" happens.

[`LiteLLMAdapter.structured()`](../../src/bismuth/adapters/llm/litellm_adapter.py)
detects the tier, embeds the JSON Schema in the prompt when it must, extracts JSON
from whatever prose arrives (fences, then a brace-balanced scan that respects
string literals), and on a validation failure **feeds the validator's own error
back as a repair turn**.

That last step is what actually rescues small models. "Field `year`: expected
string, got int" is an instruction a model can act on. "Invalid JSON" is not.

## Consequences

- Local operation is a setting, not a fork — so it stays working, because the same
  code path serves both.
- `Usage.retries` is a diagnostic: persistently non-zero for a profile means the
  model behind it is too small for the task. Surfaced rather than swallowed.
- Prompt caching is expressed as `Prompt.cache_hint` — intent, not a provider
  parameter. Placement re-sends the same charter set for every document in a batch;
  providers that support caching make the 2nd..Nth nearly free, and the rest ignore
  it via LiteLLM's `drop_params`.
- The whole engine can run against a scripted model
  ([`FakeLLM`](../../src/bismuth/adapters/llm/fake.py)) because the port is small
  enough to fake. The rules worth testing — when to refuse, when to escalate, when
  to stay quiet — are exactly the ones a real model makes untestable.

**Costs.**

- Two profiles is a crude taxonomy. A third tier (a cheap classifier) may be
  warranted; two is what we can currently justify.
- Retry logic hides model weakness behind latency and tokens. Mitigated by
  reporting retries, and by `BISMUTH_LLM_MAX_SCHEMA_RETRIES=0` to make violations
  fail loudly in development.
- LiteLLM is a large dependency and a moving target. It shipped 1.92.0 without a
  wheel, which we exclude in `pyproject.toml` — the kind of tax this dependency
  charges.

## Revisit when

We have measured quality on a real corpus with an 8B model. The claim "local works"
is currently an argument about design, not a measurement — and the honest thing
would be to publish the number.
