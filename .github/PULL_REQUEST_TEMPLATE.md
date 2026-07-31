## What this changes

<!-- The behaviour that is different afterwards, in a sentence or two. -->

## Why

<!-- The reason the current behaviour was not good enough. If this contradicts
     an ADR, say so here and add an ADR superseding it -- that is allowed and
     interesting. See CONTRIBUTING.md. -->

## Checks

- [ ] `ruff check src tests examples` and `ruff format --check src tests examples`
- [ ] `mypy`
- [ ] `pytest -q`
- [ ] No copyleft dependency added (AGPL/GPL/LGPL). See [docs/licensing.md](../docs/licensing.md).
- [ ] Dependencies still point inward — no `TID251` suppressed to make this build

## Notes for the reviewer

<!-- Anything you are unsure about, or a decision you would like argued with. -->
