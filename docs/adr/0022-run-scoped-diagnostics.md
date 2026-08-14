# 0022 — Keep compact run timelines over exact diagnostic artifacts

## Status

Accepted.

## Context

One 125-document run produced a 129.8 MB `llm.jsonl`. It preserved provider chunks but
mixed repeated prompts, tool schemas and raw transport objects in single lines as large as
8.32 MB. Agent calls had no explicit window, stage or call ID and had to be joined to the
trace by ordinal position. Tool results in the trace retained only a 200-character preview,
and the timing script omitted all agent calls.

Raw evidence was valuable; its storage and index were not suitable for routine human or
LLM analysis.

## Decision

Each server start creates an immutable `logs/runs/<run_id>/` with a manifest and normalized
timeline. Every workflow window, agent run, LLM call and tool call receives an explicit
join identity. `llm.jsonl` becomes a compact call index. Exact provider-ready requests,
rebuilt responses and full tool observations are separate JSON artifacts. Provider chunks
move to a losslessly compressed JSONL stream artifact.

The top-level text, trace and LLM index remain a disposable view of the current run for
compatibility and quick inspection. Prior run directories are not truncated. Experiments
and external server logs are not part of an operational run timeline.

## Consequences

- A debugger reads the small timeline first and opens only relevant evidence.
- Exact input, output, validation and filesystem effects remain auditable.
- Raw transport evidence is retained without dominating ordinary context.
- Artifact references and schema versions permit deterministic tooling.
- Logging writes more small files, but avoids repeating raw chunks and cumulative prompts
  in one giant JSONL record.
