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
