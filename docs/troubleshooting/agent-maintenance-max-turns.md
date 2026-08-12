# Agent maintenance ended with no visible change

## Observed incident

On 2026-08-12 a 300-document vault was retried with a tool-capable model. The HTTP
request ran for roughly 157 seconds, but no folders appeared. The durable checkpoint
incorrectly said:

```json
{"status":"done","applied":false,"moved":0,"summary":"","attempts":2}
```

The raw LLM log showed two independent planner runs. Both read the inventory and used
all 24 Agent Kit turns. They repeatedly inspected sidecars, grepped fields already in
the compact cards, and invoked the verifier more than once. Neither run called
`submit_plan`. The second run's final request contained 48 conversation messages (24
assistant turns plus tool results), and the loop stopped at its configured `max_turns`.

## Root cause

Agent Kit intentionally returns `RunResult(stopped="max_turns")` instead of raising an
exception. Bismuth checked only whether a validated boundary had been captured. An empty
boundary list was also the representation for a legitimate "no change" judgement, so
turn exhaustion was silently collapsed into successful no-op maintenance.

This was an orchestration and state-classification defect, not evidence that the model
judged the current root-only structure to be good.

## Resolution

- A no-change result now requires an explicit `finish_no_change` tool call and reason.
  Plain prose or max-turn termination is incomplete maintenance.
- A planner that stops without `submit_plan` or `finish_no_change` produces a retryable
  failed checkpoint; it can no longer become `done / moved=0`.
- The temporary 500-card whole-vault inventory fix was removed. Maintenance now receives
  at most 50 new arrivals (and at most 18,000 compact-card characters), then updates the
  tree before the next window. Existing folders are inspected only in bounded local pages.
- The verifier is a one-shot tool with its own eight-turn budget. Repeated calls are
  rejected and direct the planner to submit the plan.
- The prompt reserves tool turns for verification and submission rather than recounting
  card fields.
- Older empty-success checkpoints are migrated to retryable failure on server startup.
- The UI now shows both completed moves and explicit no-change outcomes instead of
  disappearing silently.

These are corpus-neutral execution rules. No Korean-law category, filename pattern, or
test-corpus label was added to the planner.
