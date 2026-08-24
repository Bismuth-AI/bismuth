# Contributing

Thanks for helping improve Bismuth. Bug reports, parser support, prompt improvements,
tests, and documentation fixes are all welcome.

## Development setup

```console
git clone https://github.com/Bismuth-AI/bismuth.git
cd bismuth
python -m venv .venv
```

Activate the environment, then install the project:

```console
python -m pip install -e ".[all,dev]"
```

On Windows, activate with `.venv\Scripts\activate`. On macOS or Linux, use
`source .venv/bin/activate`.

No API key is required for the automated test suite. Tests use scripted model
adapters and temporary vaults.

## Before opening a pull request

```console
ruff check src tests
ruff format --check src tests
mypy
pytest -q
```

Keep each pull request focused. Explain why the change is needed, describe any
user-visible behavior, and add tests for behavior that changed.

## Architecture

Dependencies point inward:

```text
domain/       Value objects and pure functions
ports/        Protocols used by services
services/     Application use cases
adapters/     LLM, filesystem, parser, catalog, and journal implementations
agentkit/     Internal tool-calling agent loop
container.py  Application composition root
```

Services must depend on ports rather than adapters. Ruff enforces this boundary.
When changing FastAPI, CLI, filesystem, or model adapter code, include a test that
exercises the real boundary rather than only mocking the call site.

Prompts and code comments are written in English. Keep prompt instructions generic:
do not add examples or rules tailored to one test corpus, industry, organization,
or document set.

## Dependencies and licensing

Runtime dependencies must be compatible with Apache-2.0. The project currently
accepts permissive MIT, BSD, and Apache-2.0 dependencies; discuss any exception
before adding it.

## Reporting bugs

Include the operating system, Python version, Bismuth version, configured provider
and model, input file formats, and the behavior you expected.

Run diagnostics may contain full document text, prompts, and model responses. Do not
upload `logs/` or `.bismuth/` contents without reviewing and redacting sensitive data.
Prefer a minimal reproduction with synthetic documents whenever possible.
