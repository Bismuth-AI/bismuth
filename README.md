# Bismuth

<p align="center">
  <a href="./README.md"><kbd>English</kbd></a>
  <a href="./README.ko.md"><kbd>한국어</kbd></a>
</p>

<p align="center">
  <a href="https://github.com/Bismuth-AI/bismuth/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Bismuth-AI/bismuth/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white">
  <a href="LICENSE"><img alt="Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-blue.svg"></a>
</p>

**Drop in documents. Get a real folder structure an LLM agent can navigate.**

Bismuth reads documents, creates compact document cards, and organizes the originals
into an ordinary filesystem tree. As the collection grows, it reuses existing folders,
creates useful new branches, and revisits structure that no longer fits.

The result is not a proprietary database. It is a directory of original files,
searchable Markdown sidecars, and folder notes that remains usable without Bismuth.

> Bismuth aims for a practical, navigable library—not a perfect universal taxonomy.

> [!IMPORTANT]
> Bismuth is alpha software. The complete web upload flow currently supports PDF files
> only, and interfaces may change before 1.0.

## Why Bismuth?

Tool-using LLMs can inspect paths, list folders, grep text, and read only the documents
they need. That works best when the filesystem itself provides useful navigation clues.
A flat directory does not.

Bismuth turns an unstructured collection into an agent-readable library:

- folder names narrow the search space;
- `_folder.md` explains what belongs in each branch;
- `<original>.md` makes extracted content and document metadata grep-friendly;
- the original document remains the source of truth.

There is no fixed category tree and no corpus-specific few-shot taxonomy. The structure
is inferred from the documents in the vault and the tree that already exists.

## How it works

1. An upload is validated and staged safely.
2. Bismuth parses the document and builds a structured card from its contents.
3. The card is compared with the current folder tree.
4. Documents are filed in order so each decision can use the structure built so far.
5. As evidence accumulates, Bismuth groups loose files, reshapes overly broad branches,
   and settles files that remain at the root.
6. Filesystem changes are applied through a journaled transaction and reflected in
   folder notes and Markdown sidecars.

Documents are prepared concurrently for throughput, then filed in deterministic input
order. A saved card can also be reused to rebuild the tree without parsing the original
again.

## What the vault looks like

```text
my-vault/
├── _folder.md
├── Projects/
│   ├── _folder.md
│   ├── Planning/
│   │   ├── _folder.md
│   │   ├── roadmap.pdf
│   │   └── roadmap.pdf.md
│   └── Research/
│       └── ...
└── .bismuth/
```

- `_folder.md` describes a folder's scope and helps both placement and retrieval.
- `<original>.md` contains searchable extracted text and the document card.
- `.bismuth/` stores journals and runtime metadata. Do not edit or delete it manually.

Bismuth does not rewrite original file contents. It does move originals while organizing
the vault, so paths may change. Generated sidecars and folder notes are managed by
Bismuth.

## Features

- **Corpus-driven organization** — no predefined categories or domain-specific examples.
- **Growing folder structure** — reuses, creates, groups, and revisits branches as the
  collection changes.
- **Card-based rebuilds** — clears and rebuilds folder structure from saved cards without
  paying the parsing cost again.
- **Agentic retrieval** — the library agent inspects paths and folder notes before using
  `grep` and `read` on selected documents.
- **Reversible changes** — moves, deletions, and approved reorganizations are journaled;
  interrupted transactions are recovered on the next start and completed operations can
  be undone.
- **Provider choice** — Anthropic, OpenAI, and OpenAI-compatible endpoints such as Ollama,
  LM Studio, vLLM, or an internal gateway.
- **Separate answering model** — filing and question answering may use different model
  configurations.
- **Live progress and diagnostics** — the local web UI streams ingest progress and offers
  per-run model-call traces and spend information.

## Quick start

### Requirements

- Python 3.11 or newer
- A supported hosted model API, or an OpenAI-compatible local endpoint

Bismuth is currently installed from source:

```console
git clone https://github.com/Bismuth-AI/bismuth.git
cd bismuth
python -m venv .venv
```

Activate the environment:

```bash
# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Install and launch:

```console
python -m pip install ".[all]"
bismuth
```

Bismuth opens `http://127.0.0.1:8765` by default. On first launch, the browser setup asks
for:

- the vault directory;
- the model provider and model;
- an API key, or the URL of an OpenAI-compatible endpoint.

Useful launch options:

```console
bismuth --vault ./my-vault
bismuth --port 9000
bismuth --no-open
bismuth --help
```

Settings are stored in `~/.bismuth/config.json`. For environment-based configuration,
copy [`.env.example`](.env.example) to `.env` and use the documented `BISMUTH_*`
variables.

## Model backends

The setup screen supports:

- Anthropic;
- OpenAI;
- OpenAI-compatible servers, including Ollama, LM Studio, vLLM, and internal gateways.

The default local endpoint example is `http://localhost:11434/v1`. Organization quality
depends on the model's instruction following, structured-output reliability, and context
window. Quality on small local models has not yet been benchmarked systematically.

Document text is sent to the model endpoint you configure. Choose and operate that
endpoint according to your privacy requirements.

Provider credentials are stored locally in `~/.bismuth/config.json`, not in an operating
system keychain. The file is created with user-only permissions where the platform
supports them.

## Current scope

Supported in the complete product flow:

- PDF upload and text extraction;
- up to 500 files per request, 50 MB per file, and 500 MB total;
- document cards, Markdown sidecars, and folder notes;
- incremental organization and card-based full refiling;
- manual move and delete with journal-backed undo;
- folder-aware question answering;
- per-run LLM diagnostics and usage accounting.

Not currently supported:

- OCR for scanned PDFs;
- authenticated deployment on an external network;
- background scheduled organization;
- an MCP server;
- guaranteed organization quality on small local models.

Parser adapters for additional formats exist in the codebase, but those formats are not
yet supported by the complete upload and analysis flow.

## Architecture

Bismuth uses ports and adapters. Dependency direction is enforced by Ruff rules.

```text
domain/       Value objects and pure rules
ports/        Protocols required by the application
services/     Ingest, filing, maintenance, retrieval, and transactions
adapters/     LLM, filesystem, parser, catalog, and journal implementations
prompts/      Structured, corpus-neutral model instructions
agentkit/     Internal provider-neutral tool-calling agent loop
api/          FastAPI application and local web UI
cli/          Local web application launcher
container.py  Composition root
```

Tests inject scripted models and temporary vaults, so core organization, recovery, and
API behavior can be verified without an API key.

## Data safety and diagnostics

Every filesystem mutation is journaled before it is applied. If a process stops during a
transaction, Bismuth detects the incomplete entry and rolls it back on the next start.

Diagnostics are written to `./logs/`. Per-run records under `./logs/runs/<run_id>/` are
not deleted automatically. They may contain extracted document text, prompts, model
inputs, and model outputs. Review and redact them before sharing a bug report. The logs
directory is ignored by Git.

The web application has no authentication and intentionally binds only to a loopback
address. Do not expose it directly to an external network.

## Development

```console
python -m pip install -e ".[all,dev]"
ruff check src tests
ruff format --check src tests
mypy
pytest -q
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for contribution guidelines and
[`CHANGELOG.md`](CHANGELOG.md) for release history.

## License

Bismuth is available under the [Apache License 2.0](LICENSE).
