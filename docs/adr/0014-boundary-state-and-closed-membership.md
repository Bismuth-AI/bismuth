# 0014 — Boundary state and closed membership decisions

**Status:** accepted
**Amends:** [0011](0011-bounded-maintenance-and-stable-signs.md), [0013](0013-bounded-llm-output-and-plain-placement.md)

## Context

Bounding structured output prevented runaway generation, but it did not make an
open-ended schema a good classification protocol. A guarded run formed one child
folder from a young root, immediately reviewed that single child as a mature
boundary, and repeatedly attempted a complete redesign. Failed redesigns returned
before ordinary routing and class emergence, so the tree stopped growing while LLM
cost continued to rise.

The same run showed that a model-written folder `purpose` is not durable state. The
model wrote request-local `D####` handles, exclusions, and its decision process into
the public `_folder.md`. A character limit could reject or cap the symptom, but could
not define what the text meant.

## Decision

Machine-owned folder notes are projections of structural state, not generated prose.
The parent stores the chosen axis and question; each child stores the deterministic
sign `<axis>: <class name>`. Human-owned notes remain free-form and protected.

Membership is decided one document at a time through the LLM port's closed-choice
operation:

- existing routing returns one `F###` handle or `STAY`;
- membership in a newly proposed class returns `SHELF` or `STAY`;
- complete replacement returns one `G###` handle.

No membership call returns JSON, document ID arrays, reasons, notes, or names. The
application retains the request-local mapping and builds the complete plan itself.
Calls are run with bounded concurrency so a local server is not flooded.

A folder with zero children is flat. One child is provisional: it may receive loose
documents and grow another sibling, but it is not reviewed as a complete navigation
boundary. Two or more children establish a boundary eligible for review.

Every review attempt is durable state. A failed replacement records
`last_review_at_documents` and `repair_pending`; it does not block additive filing and
is not repeated until the evidence doubles again. A successful holding review records
the same baseline and clears `repair_pending`. Review safety failures postpone only
the destructive review, never ordinary filing.

Output limits remain transport circuit breakers. They are not semantic validation and
must not determine whether a folder name, note, or classification is correct.

## Consequences

- Temporary handles and inventory narration cannot enter new managed folder notes.
- Membership output size is constant regardless of archive size.
- Complete replacement needs more small calls, but each result has one mechanically
  valid interpretation and cannot omit or duplicate document IDs.
- A rejected repair no longer freezes the library or repeats on every arrival.
- Fake-LLM functional tests still protect mechanics, not classification quality. Real
  runs must be evaluated through structure metrics and human inspection.

