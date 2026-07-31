# Changelog

Notable changes to Bismuth. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) — with the caveat
that below `1.0.0` the interface may break between minor versions.

## [Unreleased]

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
  file moves and knows its own inverse. `bismuth undo <id>` reverses any entry,
  and the undo is itself undoable. An interrupted run is recovered on next start.
- **Parsers** for `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.hwpx`, `.md`, `.txt` and
  `.csv`. HWPX is hand-written rather than taken from the AGPL library.
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
  copyleft appears transitively. See [`docs/licensing.md`](docs/licensing.md).
- Quality on small local models is **not measured**. "Local works" is an argument
  from the design (task decomposition and schema constraints), not a result. The
  ADRs say so too.

[Unreleased]: https://github.com/Bismuth-AI/bismuth/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Bismuth-AI/bismuth/releases/tag/v0.1.0
