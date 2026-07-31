# 0001 — The filesystem is the product; the database is a cache

**Status:** accepted

## Context

Bismuth produces an organised document collection. Where does that collection
*live*?

The default answer is a database: rows for documents, folders, tags, relations, and
a UI over the top. The filesystem then becomes an export target — a button that
writes a zip.

Following that path, one question keeps arriving and never gets a good answer:
*what happens when the user moves a file themselves?* They will. The inbox is a
real folder, and people drag things in file explorers. With a database as the
source of truth there are now two truths, and the code to reconcile them grows
forever.

A second question is worse: *why should anyone trust this?* An open-source tool
asking to organise a company's documents is asking for a lot. If the value is
locked inside our database, uninstalling means losing the work, and a tool that
takes hostages does not get installed twice.

## Decision

**The vault directory is the source of truth. `.bismuth/` is a cache that can be
deleted at any time.**

Concretely:

- Every card, charter, and extracted text is on disk as a file, in the vault, in
  Markdown, next to the document it describes.
- `.bismuth/` holds the journal and derived state. Everything in it is
  reconstructible by re-reading the vault (at the cost of tokens).
- The one thing that is *not* reconstructible — `axis_order`, the human's decision
  — is mirrored into the vault's root charter, on the truth side of the line.
- There is no export feature, because there is nothing to export from.

## Consequences

**What this buys.**

- The user reorganising in Finder is not a conflict to reconcile. It is a fact to
  read — and the most valuable signal we get (see [0002](0002-two-loops.md)).
- Dropbox, git, Time Machine, `rsync`, and a zip mailed to a colleague all work.
  We wrote none of that.
- An `ls`/`grep` agent reads the output with no connector, no index, no server.
  This is the whole point of the project and it falls out for free.
- Uninstalling leaves the work behind. Demonstrating there is no hostage is what
  earns the first install.
- "Delete `.bismuth/` and restart" is advice we can give without wincing — which
  stops being true the moment there is a schema to migrate.

**What it costs — and these are real.**

- *No transactions.* Filesystems have none. We built [the journal](../../src/bismuth/domain/journal.py)
  to supply atomicity of intent instead: write-ahead, roll back on failure, recover
  on restart. That is a load-bearing subsystem we would not otherwise have needed.
- *External change detection.* We need a watcher to notice what the user did.
- *Slow queries at scale.* "Every card with facet X" is a directory scan. Fine at
  10k; not fine at 1M.
- *Path portability.* Case sensitivity, path length, reserved names, Unicode
  normalisation — all now our problem, and all handled in
  [`adapters/vault/filesystem.py`](../../src/bismuth/adapters/vault/filesystem.py)
  and [`domain/facet.py`](../../src/bismuth/domain/facet.py).
- *Symlinks on Windows* need Developer Mode, which constrains secondary axis views.

## Revisit when

A vault reaches a scale where scanning is the bottleneck — measure first. The
answer then is an **index in front of these files**, not a database instead of
them. If a proposal ever requires the user to accept that their documents live
somewhere they cannot read without our software, it has misunderstood the project.
