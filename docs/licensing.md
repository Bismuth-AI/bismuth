# Licensing

Bismuth is **Apache-2.0**.

The choice is not decorative. This tool is meant to be pointed at document
collections that companies will not upload anywhere and will not let a consultant
take home — which means it has to be installable *inside* those companies, next to
proprietary code, without a lawyer stopping it. Apache-2.0 permits that and adds
an explicit patent grant; a copyleft licence would rule out the deployment we care
most about.

That decision constrains what Bismuth may depend on, and one dependency choice
below costs us real quality to keep it.

## Runtime dependencies

Every runtime dependency is MIT, BSD, or Apache-2.0. None is copyleft.

| Package | Licence | Why |
|---|---|---|
| pydantic | MIT | Domain value objects, schema-constrained model output |
| pydantic-settings | MIT | Configuration |
| litellm | MIT | One call signature across every provider — the local-model story |
| typer | MIT | CLI |
| rich | MIT | Terminal output |
| pyyaml | MIT | Charter and sidecar frontmatter |
| anyio | MIT | Structured concurrency |
| python-frontmatter | MIT | Frontmatter parsing |

### Parsers (`[parsers]` extra)

| Package | Licence | Reads |
|---|---|---|
| pypdf | BSD-3-Clause | `.pdf` |
| python-docx | MIT | `.docx` |
| python-pptx | MIT | `.pptx` |
| openpyxl | MIT | `.xlsx`, `.xlsm` |
| *(standard library)* | — | `.hwpx`, `.txt`, `.md`, `.csv` |

### Server (`[server]` extra)

| Package | Licence |
|---|---|
| fastapi | MIT |
| uvicorn | BSD-3-Clause |
| python-multipart | Apache-2.0 |
| watchfiles | MIT |

## Deliberate exclusions

### PyMuPDF (AGPL-3.0) — the expensive one

PyMuPDF is the best PDF text extractor available to Python. It is faster than
pypdf, it preserves layout, and it handles multi-column pages that pypdf
interleaves into nonsense.

We do not use it, because AGPL-3.0 is viral across a network boundary. Bismuth
ships an HTTP server. A company running that server inside their network with
PyMuPDF in the process could be obliged to publish their modifications — and a
tool that hands someone a legal problem along with their tidy folders is not a
tool they will deploy.

**What this costs, stated plainly:** pypdf reads text, not layout. Multi-column
pages interleave. Tables lose their grid. Scanned pages yield nothing — Bismuth
detects that case and raises rather than writing a confident card about a document
nobody read (`adapters/parsers/pdf.py`).

This is the single biggest quality compromise in the project. It is revisitable —
via an optional extra a user opts into with full knowledge of what they are
accepting, never as a default. That is a decision for whoever needs it, not one to
make on their behalf.

### pyhwp / hwp5 (AGPL-3.0)

Same reasoning, and the reason `adapters/parsers/hwpx.py` exists at all.

Korean offices run on 한글, and a document tool that cannot read HWP is not usable
in the places that most need one. The mature Python library for it is AGPL.

HWPX makes the problem disappear: it is the ISO-standardised ZIP-plus-XML format
that 한글 2014 and later write, so `zipfile` and `ElementTree` are the entire
dependency list. Roughly 100 lines, no licence risk, and it handles the table
nesting that a naive text scan flattens.

**What this costs:** legacy binary `.hwp` is not supported. Converting it needs
한글 itself. `bismuth doctor` says so, so the gap is found at setup rather than
mid-import.

### unstructured (Apache-2.0, but)

Permissively licensed and would replace most of `adapters/parsers/`. Not used
because it pulls a very large transitive tree — including, depending on extras,
models. Install friction is the main reason open-source tools go uninstalled, and
`pip install bismuth-kb` finishing quickly is worth more here than the formats it
would add.

## Checking this yourself

```console
pip install pip-licenses
pip-licenses --from=mixed --order=license --with-urls
```

If you find a copyleft dependency has crept in transitively, that is a bug —
please open an issue.
