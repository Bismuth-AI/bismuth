# 0030 — Hybrid agentic placement and structured local growth

## Status

Accepted, amended by ADR-0031. Supersedes ADR-0029's permission for a one-document
placement agent to create folders. It does not restore the retired fixed-size
arrival-window organizer.

## Context

The file-by-file harness had the better structural quality: documents stayed broad until
several colocated cards exposed a contrast, then structured proposal, membership, pure
validation and a semantic boundary audit created local signs and moved members atomically.
Its placement decision, however, was a closed classifier that could not inspect ambiguous
current evidence.

ADR-0029 kept the useful part of file-by-file operation but gave the placement agent
`create_child`. In real run `20260813T161356Z_4359d57ac4`, the first document created
`법률 — 대한민국 법률 문서의 수집 및 분류`. The sign positively described nearly every
later document, so 94 of 96 documents entered that one folder. The model followed its
prompt and the host accepted the action. No automatic subdivision ran because production
wiring passed `subdivision=None`.

This repeated the failure already recorded in ADR-0008: one document cannot distinguish a
broad reusable class from a corpus-wide attribute or a title-shaped singleton. Calling the
first boundary provisional does not help when the operation that could revise it is absent.

## Decision

Automatic ingestion has two deliberately unequal model contracts.

### 1. Agentic placement reads; it does not design

For one arriving document, a fresh Agent Kit transcript receives current folder handles,
routing signs and a small related-card shortlist. It may inspect addressable folders and
cards, then `finish_placement` with an existing folder or the root/current parent.

The terminal schema has no new-folder name, purpose, companion move or repair action.
Folder inspection never exposes durable catalog hashes as if they were callable document
handles. Placement's bounded transcript does not install the context-recall tool because
its entire evidence set already fits active context.

### 2. The structured harness alone grows the tree

After the document and sidecar are committed, the local maintenance service examines the
folder where it landed. With no established boundary it asks for sibling classes together
and then their members in separate native structured-output calls. The application requires:

- a named axis and immutable question;
- at least two sibling classes with at least two known members each;
- optional unclaimed documents left at the parent;
- no duplicate, ancestor-named, axis-named or spent-axis class;
- an independent semantic boundary audit of the exact candidate.

Only an accepted candidate creates the initial direct siblings and moves all their existing
members and sidecars in one journal transaction. Existing siblings constrain later proposals to
the stored axis and are audited together. Boundary replacement remains separately gated.
Maintenance failure is logged after filing and cannot turn the document into a failed
upload.

The harness runs on the actual landing parent after every arrival; ancestors receive only
due boundary review. There is no 30-document window, tail flush, global upload plan or
whole-batch rollback.

## Why this combines the useful parts

The agent contributes targeted evidence seeking, opaque capability handles, current-tree
awareness, normal root placement and per-document failure isolation. The harness contributes
contrastive multi-document evidence, constrained outputs, deterministic invariants,
same-axis siblings, semantic review and atomic movement of earlier documents.

Neither is allowed to imitate the other. Placement cannot invent taxonomy, and structure
growth cannot issue raw filesystem commands.

## Consequences

- An empty vault remains flat until an actual distinction has at least two positive
  examples and a negative remainder.
- A corpus-wide property such as “all documents of this format” cannot become the only
  child, regardless of language or domain.
- Later documents see every accepted local structural change immediately.
- Structured maintenance calls add cost after placement, but their scope is one local
  boundary and they preserve the harness behavior that produced the better trees.
- Existing vaults already polluted by ADR-0029 may require undo, clean re-ingest, or an
  explicit boundary-repair run; the new contract prevents recurrence but does not silently
  rewrite user data on startup.

## Revisit when

Two clean ingests of the same corpus in different orders still produce materially different
document groupings after every local boundary has been considered. Use pairwise co-location
rather than folder-name equality. Do not return folder creation to one-document placement.
