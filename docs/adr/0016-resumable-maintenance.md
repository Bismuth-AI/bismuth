# 0016 — Resume maintenance without re-ingesting the library

**Status:** accepted
**Amends:** [0015](0015-agentic-shadow-planning.md)

## Context

Document ingest commits before autonomous maintenance, deliberately. A 300-document
arrival therefore remained completely safe when an OpenAI-compatible server rejected
the first native tool call because tool calling was disabled. The API swallowed that
maintenance exception so it would not misreport 300 successful ingests as failures.
That isolation was correct, but the in-memory batch was then marked done and the UI had
no durable fact saying that the final structure pass had failed. Switching to a capable
model could not resume the missing stage without another ad-hoc request.

Re-uploading is not a valid recovery mechanism. It repeats parsing and cataloguing,
creates duplicate work, and confuses an already durable arrival with the failed planning
stage.

## Decision

The vault stores `.bismuth/maintenance.json` as an atomic checkpoint. It records idle,
pending, running, done, or failed status; attempt count; source; timestamps; the complete
exception message; and the last result summary. Ingest success remains independent of
this state.

The API exposes the checkpoint for observability but no longer exposes a retry operation.
The former `구조 정리 계속` and ordinary `구조 정리` retry actions were removed. Pending or
running state found after a server restart becomes a failed state, and the UI restores that
failure banner without an action button. A later upload archives the failed window as
deferred and starts only its own new arrivals; it never replays the failed window. No user
approval of the shadow plan is introduced.

Completion is also explicit. A validated `submit_plan` means a proposed change, while
`finish_no_change` records a reasoned no-op. Reaching the Agent Kit turn limit, returning
empty text, or ending without either tool is failed maintenance rather than a successful
no-op. This distinction prevents orchestration exhaustion from disappearing as
`done / moved=0`.

## Consequences

- A provider or model can be changed in settings and the interrupted structure stage can
  continue against the same 300 already-catalogued documents.
- Browser and server restarts no longer erase the recovery affordance.
- A maintenance failure is visible without being counted as an ingest failure.
- The checkpoint contains diagnostics, while raw provider chunks remain in the LLM log.
- Batch state itself is still process-local; only the independently retryable maintenance
  boundary needs to survive a restart.

## Revisit when

- maintenance is split into multiple independently resumable hierarchy passes; or
- more than one process is allowed to mutate the same vault.
