# Git workflow

The rules below are what this repository actually does. They exist so that
`git log` stays readable a year from now, and so that a reviewer can tell what a
change was for without opening the diff.

## Branches

Git Flow, minus the release branches.

```
main      released versions only. the one branch that carries tags (vX.Y.Z).
develop   the integration branch. day-to-day work lands here.
feat/*    cut from develop, merged back into develop.
hotfix/*  cut from main, merged into both main and develop.
```

Neither long-lived branch is committed to directly. Work arrives through a
short-lived branch that is merged and then deleted, and `main` moves only at a
release — so anything tagged on `main` is a version that was actually cut, not
whatever happened to land last.

`release/*` is deliberately absent. It earns its keep when stabilising a release
takes days of its own: bugs fixed against the release candidate while the next
feature set already moves on in `develop`. Bismuth does not have that problem
yet, and adding the branch now would buy a third merge step and nothing else.
When release preparation starts taking real time, add it then.

| Prefix    | For                                              |
| --------- | ------------------------------------------------ |
| `feat/`   | a capability the program did not have before     |
| `fix/`    | a defect on `develop`                            |
| `hotfix/` | a defect in a released version, cut from `main`  |
| `docs/`   | documentation and ADRs only                      |
| `test/`   | tests without a behaviour change                 |
| `chore/`  | tooling, CI, licences, repository housekeeping   |
| `build/`  | packaging and dependency changes                 |

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

## Ownership

The codebase is split by architectural layer, and each layer has one owner who
carries it end to end. The split follows the dependency rule rather than cutting
across it, so a change usually sits inside a single area.

| Area                                                      | Owner |
| --------------------------------------------------------- | ----- |
| `domain/`, `ports/`, `services/`, `container.py`, ADRs     | 최수환 |
| `adapters/`, `prompts/`, `packages/agentkit/`              | 김지홍 |
| `api/`, `cli/`, `tests/`, `.github/`, user-facing docs     | 이민재 |

A change that crosses two owners' areas is split into one commit per area —
otherwise the history stops saying who did what. `git add <path>` is enough.

Work genuinely done by two people keeps one author and adds the other as a
trailer:

```
Co-authored-by: 김지홍 <wlghd99589@gmail.com>
```

## Merging

Feature branches merge with `--no-ff`, always:

```console
git switch develop
git merge --no-ff feat/agentic-placement
git branch -d feat/agentic-placement
```

The merge commit is the point. It keeps the branch visible in the graph, groups
the commits that belonged together, and gives a single revert target if the
whole feature has to come out. A fast-forward would flatten all of that into an
undifferentiated line.

Neither `main` nor `develop` is rebased or force-pushed — their history is
shared. Rebasing a feature branch onto `develop` before merging is fine and
encouraged.

## Releases

`develop` merges into `main`, which is then tagged `vMAJOR.MINOR.PATCH`, with
[`CHANGELOG.md`](../CHANGELOG.md) updated in the same change. Because `main`
moves only here, every commit on it is a version someone decided to cut. While
the version stays below `1.0.0` the interface may break between minors, which is
what alpha means and why the README says so.

A `hotfix/*` is cut from `main`, merged back into `main` with a patch tag, and
then merged into `develop` as well. Skipping the second merge is the classic way
to reintroduce the bug in the next release.
