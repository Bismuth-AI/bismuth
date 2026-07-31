# 0002 — Split the fast loop from the slow loop

**Status:** accepted

## Context

Bismuth has to do two things that look like one thing:

1. file documents as they arrive, and
2. keep the structure good as the collection grows.

The obvious design does both in one pass: on each ingest, look at the collection,
work out the best structure, apply it. Self-improving. It demos beautifully.

It is also unusable, and the reason is not accuracy — it is that a structure which
rearranges itself whenever a file drops in is a structure nobody can hold in their
head. The user who filed something in `제안서/` yesterday and finds it in
`2025/제안서/A사/` today has learned that this tool does things behind their back.
They do not file a bug. They stop trusting it, then they stop using it, and the
telemetry shows nothing at all.

Meanwhile structure genuinely does go stale, and ignoring that is how you end up
with the folder holding 400 files that made everyone give up on the tree in the
first place.

## Decision

**Two loops with different rights.**

**The fast loop** ([`services/ingest.py`](../../src/bismuth/services/ingest.py)) runs
on every arriving file. It may: file the document, create a folder for a new facet
value, write that folder's charter, write the sidecar. It may **not** change the
shape of the tree. Nothing that existed yesterday moves today.

Creating a new folder is not an exception. A new project getting a folder is the
*existing rule* being applied, not a new rule. The axes are untouched and a user
who understands "projects get folders" is not surprised.

**The slow loop** ([`services/pressure.py`](../../src/bismuth/services/pressure.py))
runs occasionally, proposes, and waits. It may propose anything — subdividing,
new facets, reordering axes — and may do none of it without an explicit yes.

Between them sits evidence. The slow loop does not re-derive the structure and
hope; it watches for named symptoms and accumulates them:

| signal | what it means |
|---|---|
| `FOLDER_OVERFLOW` | too many files in one folder. Obvious, and the least informative. |
| `INBOX_BACKLOG` | documents the taxonomy cannot describe. **The most valuable signal we get** — a facet is missing, and the stuck documents are a worked example of it. |
| `CHARTER_COLLISION` | two folders keep both nearly-claiming a document. The axes overlap: a structural defect that worsens with growth. |
| `USER_CORRECTION` | a human moved what we filed. Their model and our rule disagree, and theirs is correct by definition. |
| `FACET_UNUSED` | an axis where 95% of documents share one value. A folder level that adds depth and no information. |

Signals accumulate; they do not fire.

## Consequences

- **Stability outranks accuracy in the fast loop.** A slightly worse folder that
  stays put beats a better one that moves. This is a deliberate inversion of what
  a benchmark would tell us to do.
- **The two-week decision gets paid in instalments.** Five seconds at a time, when
  the evidence for it is already on the table — rather than two weeks up front,
  when it is not.
- **Most scans produce no proposals.** That is success. The failure mode to design
  against is not asking too little; it is asking too much. A person who reads three
  proposals saying "your documents could be better organised" stops reading the
  fourth, and from then on the human in the loop is a rubber stamp that launders
  our mistakes as consent. Hence: `Proposal.is_actionable()`, one signal per
  subject, silence means no, and rejection clears the evidence so we never ask
  twice.
- **A user's manual move is evidence, not noise.** We get it free from the journal
  ([0001](0001-filesystem-is-the-product.md) is what makes it observable at all).
  The point is not to copy the move — it is to learn the *rule* behind it, or we
  relearn the same lesson forever.
- **The inbox is a feature.** A document Bismuth declines to place is the corpus
  telling us the taxonomy has a hole. Guessing would destroy that signal *and*
  pollute the tree.

**Costs.**

- Two code paths for what a user thinks of as one behaviour.
- A vault can sit in a bad structure indefinitely if nobody answers the proposals.
  We accept this: the alternative is acting without consent.
- Signal thresholds are guesses. They need real vaults over real months to tune,
  and until then they are configuration, not truth.

## Revisit when

We have data on how many proposals get accepted. If acceptance is very high, we
are asking too timidly. If it is very low, we are generating noise and the fatigue
problem has already started. Neither number is knowable from a demo — only from
someone living with it for six weeks.
