# Archived records — agentic organizer line (0015–0033)

**These are not current decisions. Nothing here governs the code on this branch.**

They are kept because they document a line of work that was tried against a real corpus
and abandoned, and because the failures they record are the reason several current
contracts exist. Deleting them would leave the current design looking like a preference
rather than a conclusion.

The records below were written on `feat/agentic-organizer-experiments`, where a global
planner agent and then a per-document placement agent replaced the structured
multi-document harness. Measured against real runs, neither produced better structure
than the harness it replaced. The evidence and the conclusion are in
[`../../troubleshooting/2026-08-14-classification-approach-retrospective.md`](../../troubleshooting/2026-08-14-classification-approach-retrospective.md).

## How to read these

- **As evidence of what failed, yes.** A specific run, a specific corpus, a specific
  observed failure.
- **As a decision to implement, no.** Their `Status: accepted` refers to the branch they
  were written on. On this branch they are superseded in full.
- **Do not port code from them.** Reusable infrastructure is adopted only through a
  current ADR that states why it is semantically neutral. One such port has happened so
  far: run-scoped diagnostics, from `0022` here to
  [`../0015-run-scoped-diagnostics.md`](../0015-run-scoped-diagnostics.md).

The current numbering continues from `0014` in the parent directory. Numbers collide
with the files here; that is why these live in their own directory and are referenced by
path, not by number.

| # | Record | Why it is here |
|---|---|---|
| 0015 | Agentic shadow planning | Start of the global planner line |
| 0016 | Resumable maintenance | |
| 0017 | Incremental arrival windows | The 30-document window queue |
| 0018 | Addressable agent context | |
| 0019 | Exact candidate semantic review | |
| 0020 | Repair routing and sticky findings | |
| 0021 | Language and family invariants | **Contains the domain leak** — Hangul-ratio branching and generic-word lists |
| 0022 | Run-scoped diagnostics | **Ported** — see `../0015-run-scoped-diagnostics.md` |
| 0023 | Family-cohesive retry windows | |
| 0024 | Symmetric window evidence and progressive acceptance | |
| 0025 | Family-visible repairable incremental windows | |
| 0026 | Staged atomic agent planning | |
| 0027 | Capability-scoped incremental plans | |
| 0028 | Terminal phases and dynamic model slots | |
| 0029 | Incremental agentic placement | The per-document folder-creating agent; one document created `법률`, which absorbed 94 of 96 |
| 0030 | Hybrid agentic placement, structured growth | |
| 0031 | Contrastive first boundaries | |
| 0032 | Retry declined boundaries on evidence growth | |
| 0033 | Final contrast recovery | Last state before the retrospective |
