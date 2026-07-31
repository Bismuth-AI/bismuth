# Contributing

## Getting set up

```console
git clone https://github.com/Bismuth-AI/bismuth
cd bismuth
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[parsers,server,dev]"
pytest -q
```

**No API key needed, for anything.** The whole engine runs against a scripted model
([`FakeLLM`](src/bismuth/adapters/llm/fake.py)). To see what it produces:

```console
python examples/offline_demo.py
```

## Before you open a PR

```console
ruff check src tests examples --fix
ruff format src tests examples
mypy
pytest -q
```

## The dependency rule

```
domain/     value objects, pure functions. no I/O, no model, no filesystem.
ports/      Protocols. the only things services may depend on.
services/   the use cases. know ports, never adapters.
adapters/   LiteLLM, the filesystem, the parsers, the journal.
container.py  the one place that knows both halves.
```

Dependencies point inward. This is enforced by ruff's `banned-api`, not by
reviewers noticing ??if you get `TID251`, the fix is almost always to move the
thing you need into `domain/`, not to add an ignore.

## What we would like help with

- **Parsers.** Formats we cannot read are documents we cannot organise. See the
  licence constraint below before reaching for a library.
- **Prompts.** [`src/bismuth/prompts/`](src/bismuth/prompts/) is the highest-leverage
  code in the repo and it is currently tuned by one person's intuition. If you run
  Bismuth on a real corpus in your language and the cards come out wrong, that is a
  valuable bug report even without a fix.
- **Small-model quality.** The claim "local models work" rests on task decomposition
  and schema constraints ([ADR-0004](docs/adr/0004-llm-provider-abstraction.md)). It
  is an argument about design, not a measurement. Measuring it would be the single
  most useful contribution available.
- **Slow-loop signals.** The thresholds in [`config.py`](src/bismuth/config.py) are
  guesses. They need real vaults over real months.

## Licences: the one hard rule

**No copyleft dependencies.** Bismuth is Apache-2.0 so it can be installed inside
companies next to proprietary code ??that deployment is the whole point, and a viral
licence rules it out. CI fails on AGPL/GPL/LGPL appearing anywhere in the tree.

This has cost us real quality: PyMuPDF is a better PDF extractor than pypdf and we
do not use it, and we hand-wrote an HWPX parser rather than take the AGPL one.
[`docs/licensing.md`](docs/licensing.md) explains both. If a permissive alternative
exists for something we currently do badly, that is a very welcome PR.

## Tests

Test the decisions, not the plumbing. The interesting rules are the ones about
restraint ??when Bismuth refuses to place a document, when it escalates to
judgement, when the slow loop stays quiet ??and they are exactly the rules a real
model makes untestable. That is what `FakeLLM` is for.

A test asserting that we called the model is not interesting. A test asserting that
we *declined to guess* when the model gave us half an answer is the product.

If you are touching a framework boundary (FastAPI, Typer), write a test that makes a
real call. That layer is where type checking stops helping ??a dependency alias in
the wrong scope once turned every endpoint into a 422 while types, lint and every
unit test stayed green.

## Changing a decision

Design decisions live in [`docs/adr/`](docs/adr/) with their costs and the conditions
that would overturn them. If your change contradicts one, that is allowed and
interesting ??but say so in the PR, and add an ADR superseding it. Superseded ADRs
stay; the reasoning is the point, including the reasoning we abandoned.

## Reporting bugs

Vaults contain documents you probably cannot share, so please include instead: the
shape (how many files, what formats, roughly what facets), the model you configured,
the relevant lines from `.bismuth/journal.jsonl`, and what you expected to happen.

If Bismuth moved a file somewhere wrong, `bismuth log` shows the rationale it used.
That line is the most useful thing you can paste.
