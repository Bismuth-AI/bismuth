# 0021 — Derive language and document-family invariants from the archive

**Status:** Accepted

## Context

A Korean legal corpus produced English root and child names even though the planner
prompt required the documents' own language. Both semantic critics accepted the plan
because language was only an instruction, not an application invariant.

The same run split editions of `방송통신발전 기본법` between a parent and child shelf,
and split `기술보증기금법` from its enforcement decree across root domains. A singleton
industrial-policy document was also placed in a strategic-technology shelf after its own
one-document shelf was deterministically rejected. Renaming the bad bucket made prior
semantic findings disappear because finding identity included the destination name.

Finally, a valid root repair needed to add a new sibling while routing another loose
arrival into an existing sibling. The host rejected two boundary objects with the same
parent even though both changes shared the established axis and could be atomic.

## Decision

The application derives two corpus-neutral invariants from durable sidecar metadata:

1. **Writing system.** For a create/replace boundary, its axis, question, and new class
   names must use the dominant Unicode writing system of the documents below the parent.
   `add_sibling` applies the check only to new names so an old incorrectly named boundary
   can still be repaired. No language vocabulary or configured locale is introduced.
2. **Document family.** A card title is family evidence only when the source filename
   independently begins with that title after Unicode letter/digit normalization. Exact
   titles and sufficiently substantial prefix titles form a family. A submitted plan that
   touches a family may not leave its members on different direct shelves.

Validation never silently retargets a submitted family member. It checks the exact submitted
membership and rejects an explicit split with concrete family and target evidence. A critic
must see the same candidate the planner believes it submitted; otherwise a correct revision
appears to be a stale critic failure. Family-aware window packing makes currently retryable
mates addressable together so the planner can repair the split explicitly.

In the fast placement loop, an arriving edition or grounded subordinate instrument follows
the current shelf of its existing family before an LLM is asked. If earlier family members
are already split, the new member stays at their lowest common ancestor instead of choosing
another inconsistent descendant.

Relationship findings (`overlap`, containment, duplicate boundary, and family split) over
multiple cited documents are keyed by their co-membership partition, not literal folder
names. Placement findings such as `mixed_axis`, `forced_fit`, and `level_mismatch` are keyed
by the cited documents' concrete destinations. Moving cited documents to a corrected target
therefore clears the stale placement finding, while merely renaming an overlapping group
does not clear a relationship finding. Every changed candidate is still semantically
reviewed.

One `add_sibling` plan may also route loose documents into existing siblings. If Qwen emits
that safe composite as separate `add_sibling` and `route_existing` objects for the same
parent, the host coalesces them before validation. Other same-parent operation mixtures
remain invalid.

The critic prompts explicitly treat singleton documents left at the parent as safe and
call out hiding a singleton with an unrelated document or over-partitioning into
near-family shelves.

## Consequences

- A model cannot create Latin-only folder names for a strongly Hangul corpus merely
  because the system prompt is written in English.
- Editions and base/subordinate instruments are protected both during batch maintenance
  and arrival placement.
- The invariant is conservative: ambiguous titles that are not grounded in their source
  filename do not become families, and mixed-script corpora with no dominant system do
  not receive a language rejection.
- Family repair remains possible with `rehome_existing`; only emptying a direct sibling
  requires a boundary-changing operation. Empty managed descendants are ordinary cleanup.
- A rejected candidate followed by prose preserves the current tree as an explicit skipped
  maintenance outcome; it does not convert safely filed documents into a failed batch.
- Semantic finding kinds are role-scoped by the host. Boundary critics may report sibling
  duplication; membership critics may not misuse `duplicate_boundary` for duplicate files
  co-located inside one target. Such out-of-role findings are traced and ignored.
- Manual retry retains the same bounded 30-document windows as normal ingest. Planner
  and critic turn ceilings bound each window; retries must improve reuse of the unchanged
  root context rather than bypassing the window ceiling with one oversized global pass.
- Semantic review is still responsible for domain meaning, granularity, and navigation
  value. Deterministic code protects only evidence that can be derived without embedding
  a taxonomy.
