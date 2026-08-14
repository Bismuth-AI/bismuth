# Architecture decision records

**These records answer *why*. What has to be true is in [SPEC.md](../../SPEC.md),
which outranks them — an ADR that contradicts the spec is the one that is wrong.**

Each file records one decision, what it costs, and what would make us change our
minds. They are here because the interesting thing about this project is not the
code — it is the handful of judgement calls the code is a consequence of, and
those are invisible in a diff.

Format: context, decision, consequences, and the conditions under which the
decision should be revisited. Superseded records stay; they are not deleted.

| # | Decision |
|---|---|
| [0001](0001-filesystem-is-the-product.md) | The filesystem is the product; the database is a cache |
| [0002](0002-two-loops.md) | ~~Split the fast loop from the slow loop~~ (superseded by 0007) |
| [0003](0003-derived-paths-not-chosen-folders.md) | ~~Derive paths from facet values~~ (superseded by 0007) |
| [0004](0004-llm-provider-abstraction.md) | ~~Route by profile, not by model name~~ (superseded by 0012) |
| [0005](0005-humans-choose-the-axis-order.md) | ~~The human chooses the axis order~~ (superseded by 0007) |
| [0006](0006-bismuth-owns-its-configuration.md) | Bismuth owns its configuration; the app is the settings UI |
| [0007](0007-agentic-placement.md) | Place agentically: the model reads the folder tree and picks or creates *(amended by 0008)* |
| [0008](0008-place-broadly-then-subdivide.md) | Place broadly, then subdivide; a first placement is provisional |
| [0009](0009-model-proposes-the-library-validates.md) | The model proposes classification changes; the library validates before applying |
| [0010](0010-corpus-neutral-complete-boundaries.md) | Keep prompts corpus-neutral; replace reviewed boundaries completely |
| [0011](0011-bounded-maintenance-and-stable-signs.md) | Keep routing signs stable; review complete boundaries in bounded contexts |
| [0012](0012-one-model.md) | Use one configured model for every task |
| [0013](0013-bounded-llm-output-and-plain-placement.md) | Use a plain choice for placement; bound every LLM generation |
| [0014](0014-boundary-state-and-closed-membership.md) | Derive managed signs; model membership as closed per-document choices |
| [0015](0015-agentic-shadow-planning.md) | Plan holistically with tools; validate and atomically apply the shadow plan without HITL |
| [0016](0016-resumable-maintenance.md) | Persist maintenance failure and retry only structure planning after a model change |
| [0017](0017-incremental-arrival-windows.md) | Update the tree between bounded arrival windows instead of reviewing the whole batch |
| [0018](0018-addressable-agent-context.md) | Bound active context, archive exact tool observations, and reserve plan submission turns |
| [0019](0019-exact-candidate-semantic-review.md) | Separate structural rights and semantically review the exact validated candidate |
| [0020](0020-repair-routing-and-sticky-findings.md) | Repair existing sibling misroutes and keep blocking findings sticky across one revision |
| [0021](0021-language-and-family-invariants.md) | Derive writing-system and document-family invariants from archive evidence |
| [0022](0022-run-scoped-diagnostics.md) | Keep compact run timelines over exact diagnostic artifacts |
| [0023](0023-family-cohesive-retry-windows.md) | Keep retry windows family-cohesive and review exact plans |
| [0024](0024-symmetric-window-evidence-and-progressive-acceptance.md) | Give planner and critics symmetric evidence; preserve uncontested progress |
| [0025](0025-family-visible-repairable-incremental-windows.md) | Make families visible and repairable across incremental windows |
| [0026](0026-staged-atomic-agent-planning.md) | Stage exploration/conclusion and make family assignment atomic |
| [0027](0027-capability-scoped-incremental-plans.md) | Separate reference evidence from bounded action capabilities |
| [0028](0028-terminal-phases-and-dynamic-model-slots.md) | Make evidence phases terminal and reserve model input/output slots dynamically |
| [0029](0029-incremental-agentic-placement.md) | Pivot automatic ingestion from arrival windows to one-document agentic placement |
| [0030](0030-hybrid-agentic-placement-structured-growth.md) | Keep agentic evidence-seeking placement, but let only the multi-document harness create structure |
| [0031](0031-contrastive-first-boundaries.md) | Establish a new axis with sibling contrast, immutable questions and finite placement evidence |
| [0032](0032-retry-declined-boundaries-on-evidence-growth.md) | Retry declined first boundaries only after materially new evidence |
| [0033](0033-final-contrast-recovery.md) | Recover incremental blind spots once, against the completed sibling contrast |

Comparative experiment results and the 2026-08-14 corpus-neutrality audit are recorded in
[`docs/troubleshooting/2026-08-14-classification-approach-retrospective.md`](../troubleshooting/2026-08-14-classification-approach-retrospective.md).
