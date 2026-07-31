# Git workflow

The rules below are what this repository actually does. They exist so that
`git log` stays readable a year from now, and so that a reviewer can tell what a
change was for without opening the diff.

## Branches

`main` is always releasable. Nothing is committed to it directly; every change
arrives through a short-lived branch that is merged and then deleted.

| Prefix   | For                                              |
| -------- | ------------------------------------------------ |
| `feat/`  | a capability the program did not have before     |
| `fix/`   | a defect in released behaviour                   |
| `docs/`  | documentation and ADRs only                      |
| `test/`  | tests without a behaviour change                 |
| `chore/` | tooling, CI, licences, repository housekeeping   |
| `build/` | packaging and dependency changes                 |

Branch names describe the outcome, not the author or the ticket:
`feat/agentic-placement`, not `feat/sh-work` or `feat/issue-12`.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/), with the scope
naming the layer or module the change lands in:

```
feat(placement): place from the first file, with no fixed axes

The first document creates the first folder; every one after it is placed
against the structure built so far. Below the confidence threshold Bismuth
declines and the document waits in _inbox rather than being guessed at.
See ADR-0007.
```

The subject says what the change does, in the imperative, under about seventy
characters. **The body says why.** A diff already shows what changed, so a body
that restates it is wasted; the reasoning is the part that cannot be recovered
later. When the reasoning is long enough to matter to the design, it belongs in
[`docs/adr/`](adr/) and the commit links to it.

One commit is one idea. A branch that adds a parser and also renames a
configuration key is two commits, so that either can be reverted alone.

## Merging

Feature branches merge with `--no-ff`, always:

```console
git switch main
git merge --no-ff feat/agentic-placement
git branch -d feat/agentic-placement
```

The merge commit is the point. It keeps the branch visible in the graph, groups
the commits that belonged together, and gives a single revert target if the
whole feature has to come out. A fast-forward would flatten all of that into an
undifferentiated line.

`main` is never rebased or force-pushed — its history is shared. Rebasing a
branch onto `main` before merging is fine and encouraged.

## Releases

Tagged `vMAJOR.MINOR.PATCH` on `main`, with [`CHANGELOG.md`](../CHANGELOG.md)
updated in the same change. While the version stays below `1.0.0` the interface
may break between minors, which is what alpha means and why the README says so.
