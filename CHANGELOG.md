# Changelog

Notable changes to Bismuth. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) — with the caveat
that below `1.0.0` the interface may break between minor versions.

## [Unreleased]

### Added

- **Simple-batch organization.** New uploads and card-based replay now use the same
  sequential filing path. Each document is evaluated against the structure produced by
  the documents before it, and mature vaults refile files left at the root.
- **Bounded structural review.** The organizer may reuse folders, create new folders,
  move documents, and revise an existing subtree through validated plans.
- **Agent context budgeting.** The internal agent loop compacts old tool results and
  finishes with a tool-free answer when its token budget is exhausted.
- **Run-scoped diagnostics.** Exact model requests, responses, streams, and normalized
  timeline events are retained under `logs/runs/<run_id>/` for debugging.
- **File-scoped grep.** Retrieval tools can search one document and report grouped,
  bounded matches.

### Changed

- The CLI now has one public command: `bismuth` starts the local web application.
- Web uploads are explicitly limited to PDF while other parsers remain experimental.
- Prompts and prompt-facing schemas use English instructions and avoid corpus-specific
  examples.
- The tool-calling loop is now an internal `bismuth.agentkit` module and ships in the
  `bismuth-kb` wheel instead of requiring a separate local package installation.
- Runtime logs are never removed by an automatic run-count retention policy.
- Configuration isolates filing and chat endpoints and redacts credentials in diagnostic
  output.

### Removed

- The former placement, subdivision, redesign, and maintenance pipelines superseded by
  simple-batch organization.
- Early CLI subcommands for direct ingest, inspection, maintenance, and recovery.
- The offline example and local demo pages from the public distribution.

## [0.1.1] — 2026-08-02

No change to what Bismuth does. This release exists because 0.1.0 shipped a test
suite that only passed by accident, and a repository that could not prove it.

### Fixed

- **The test suite could not be collected on a clean checkout.** Six test modules
  import helpers from each other, which needs `tests/` to be a package. Without
  `tests/__init__.py` it resolved only under `python -m pytest`, which puts the
  working directory on `sys.path`; the `pytest` console script does not, so every
  CI job died during collection while the same suite passed locally.

### Changed

- CI runs one platform — Windows on Python 3.11 — instead of a three-OS,
  two-version matrix. Bismuth's failure mode is path handling, and reserved
  names, case-insensitivity and cp949 consoles are Windows-only bugs on the OS
  the primary users run. Type checking and the copyleft audit still run
  separately.
- Branching is Git Flow without release branches: `main` carries released
  versions and their tags, `develop` carries day-to-day work.

## [0.1.0] — 2026-07-31

First alpha. Placement, sidecars, folder notes, deletion and undo work and are
tested; OCR, periodic restructuring of large vaults, and the MCP server are not
built yet.

### Added

- **Agentic placement.** A document is placed against the folder structure that
  exists at that moment, with no predefined categories and no fixed tree depth.
  The first document creates the first folder. Below the confidence threshold
  Bismuth declines and the document waits in `_inbox/` rather than being guessed
  at.
- **Folder notes (`_folder.md`).** One line saying what a folder holds, refreshed
  as its contents drift. This is what stops the same contract landing in both
  `legal/contracts` and `contracts/legal`.
- **Sidecars (`<original>.md`).** Greppable text beside every original, with a
  header naming the document it came from, so a matching line is never an orphan
  fragment. Originals are never modified.
- **A journal, and undo for everything.** Every batch is written down before any
  file moves and knows its own inverse. An interrupted run is recovered on next start.
- **Parsers** for `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.hwpx`, `.md`, `.txt` and
  `.csv`.
- **Provider abstraction.** Services request a model *profile* (`FAST`,
  `REASONING`) and never name a model, so running fully local through Ollama
  changes no code path. Every model call is schema-constrained and retried on
  schema failure.
- **Setup in the browser.** Bismuth asks the provider which models the supplied
  key can actually reach, so there is nothing to look up and no prefix to get
  wrong. Answers are stored in `~/.bismuth/config.json`; `BISMUTH_*` environment
  variables cover Docker and CI.
- **A file-explorer UI** and a CLI (`add`, `tree`, `log`, `undo`, `doctor`).
- **Retrieval** that walks the vault the way a capable new hire would — reads the
  folder notes, forms a hypothesis, opens files.
- **`agentkit`**, the provider-agnostic tool-calling loop, as a separate
  installable package under `packages/agentkit`.
- **An engine that runs with no API key.** `build(settings, llm=FakeLLM())` wires
  the whole thing against a scripted model, which is how the test suite and CI
  run: no key, no network, no cost.

### Notes

- Every runtime dependency is MIT, BSD or Apache-2.0, and CI fails if anything
  copyleft appears transitively.
- Quality on small local models is **not measured**. "Local works" is an argument
  from the design (task decomposition and schema constraints), not a result.

[Unreleased]: https://github.com/Bismuth-AI/bismuth/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/Bismuth-AI/bismuth/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Bismuth-AI/bismuth/releases/tag/v0.1.0
