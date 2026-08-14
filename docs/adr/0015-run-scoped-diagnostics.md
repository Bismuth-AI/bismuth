# 0015 — Keep run-scoped joinable diagnostics; a compact timeline over exact artifacts

**Status:** accepted
**Ported from:** [`archive/0022-run-scoped-diagnostics.md`](archive/0022-run-scoped-diagnostics.md),
written on the abandoned agentic-organizer branch. The mechanism is semantically neutral;
none of that branch's classification decisions come with it.

## Context

[SPEC.md §6.3](../../SPEC.md) requires that a cause be reconstructed from what the model
was actually given and actually returned. The logging this branch had could not support
that claim.

`logs/` held three files, all truncated on start. A restart destroyed the evidence for
the run before it — which is the run someone is asking about. The only join key was
`document_id`, so a call could be attributed to a document but not to the stage, window,
or packet it was made for; with reading concurrent, line order no longer implied anything.

Whole provider streams were serialised into the same record as the prompt. One 125-document
run produced a 129.8 MB `llm.jsonl` with single lines of 8.32 MB. The evidence was
preserved and effectively unreadable, which is not the same as preserved.

## Decision

Each start creates `logs/runs/<run_id>/` and does not truncate the runs before it. The
top-level `bismuth.log`, `trace.jsonl` and `llm.jsonl` remain a disposable view of the
current run; `logs/latest.json` points at the durable one.

A run directory holds a manifest (version, platform, resolved model and generation
settings), a normalized `timeline.jsonl`, a compact `llm.jsonl` call index, and the exact
evidence each index line references:

```text
calls/<call_id>.request.json    provider-ready input
calls/<call_id>.response.json   rebuilt output, usage, finish reason
streams/<call_id>.jsonl.gz      full provider chunks, compressed
tools/<artifact_id>.json        full tool observation with its sha256
```

Execution identity is carried in a ContextVar and attached to every line: `run_id`,
`document_id`, `stage`, `window_id`, `call_id`. The pipeline sets it at the boundaries
that already exist — each card window, each level of the placement descent, each
subdivision stage, and each bounded evidence packet of a review or replacement. The LLM
adapter is several layers below whoever knows which stage is running and must not learn
about stages to record one.

Model and generation settings go into the manifest because a conclusion about a setting
is unverifiable if the run never recorded it. Credentials, URL userinfo and query strings
never do.

## Consequences

- A debugger reads a small timeline first and opens only the evidence in question.
- A restart no longer destroys the run being asked about.
- Raw transport evidence is retained without dominating ordinary context.
- Filtering the timeline by `document_id` still reconstructs one document end to end;
  filtering by `stage` or `window_id` now isolates one decision inside it.
- A boolean merged fail-closed across packets can be traced to the packet that failed.
- Logging writes more small files. That is the cost of not repeating raw chunks and
  cumulative prompts inside one giant record.
- The compact index no longer carries prompts or attempts inline, so anything reading
  `llm.jsonl` for them must follow `request_ref` / `response_ref` instead.

## Revisit when

Run directories accumulate without bound; nothing prunes them yet, deliberately — the
first thing an automatic cleanup would delete is old evidence. Revisit when disk becomes
a real constraint, and prefer compressing or archiving whole runs over sampling inside one.
