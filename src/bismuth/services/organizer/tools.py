"""Read-only, capability-scoped vault tools exposed to librarian agents."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Literal

from agentkit import FunctionTool, Tool
from pydantic import BaseModel, Field

from bismuth.domain.charter import CHARTER_FILENAME
from bismuth.domain.document import sidecar_name
from bismuth.ports.catalog import Catalog
from bismuth.ports.vault import INBOX, Vault
from bismuth.services.charters import CharterService
from bismuth.services.maintenance_windows import family_components
from bismuth.services.organizer.prompts import _GREP_MATCH_LIMIT, _TEXT_SUFFIXES, PlanOperation
from bismuth.services.sidecar import read_sidecar_meta


class _LsArgs(BaseModel):
    path: str = Field(
        default="",
        description="Vault-relative folder path; empty or / means the vault root.",
    )
    offset: int = Field(default=0, ge=0, description="First document to return (0-based).")
    limit: int = Field(default=50, ge=1, le=100, description="Documents to return per page.")


class _TreeArgs(BaseModel):
    path: str = Field(
        default="", description="Vault-relative folder; empty or / means the root."
    )


class _ArrivalsArgs(BaseModel):
    pass


class _InventoryArgs(BaseModel):
    path: str = Field(
        default="", description="Vault-relative folder; empty or / means the root."
    )
    recursive: bool = Field(default=True, description="Include documents in descendants.")
    offset: int = Field(default=0, ge=0, description="First document to return (0-based).")
    limit: int = Field(default=50, ge=1, le=50, description="Document cards per page.")


class _ReadArgs(BaseModel):
    path: str = Field(description="A D-prefixed movable or R-prefixed reference handle.")
    offset: int = Field(default=0, ge=0, description="First line to return (0-based).")
    limit: int = Field(default=200, ge=1, le=2000, description="How many lines to return.")


class _GrepArgs(BaseModel):
    pattern: str = Field(description="Regular expression to search for.")
    path: str = Field(default="", description="Folder to search under, or empty for the root.")


class _NoteArgs(BaseModel):
    folder: str = Field(default="", description="Folder whose note to read.")


class _RelatedArgs(BaseModel):
    document: str = Field(
        description="D-prefixed movable or R-prefixed reference handle to compare."
    )
    path: str = Field(default="", description="Optional folder scope, or empty for the vault.")
    limit: int = Field(default=12, ge=1, le=30)


class _PlanMove(BaseModel):
    document_ids: list[str] = Field(
        min_length=1,
        description=(
            "Assignment handles shown by arrivals/inventory. Use an F-prefixed family "
            "unit instead of any of its D-prefixed members; ordinary documents use D handles."
        ),
    )
    target: str = Field(
        description=(
            "One class name below the boundary parent (preferred), or its full vault-relative "
            "path. The child may be new; submit_plan creates accepted new shelves."
        )
    )


class _BoundaryPlan(BaseModel):
    parent: str = Field(default="", description="Existing folder whose direct boundary changes.")
    operation: PlanOperation = Field(
        default="create_boundary",
        description="The structural right this plan needs; choose the least powerful one.",
    )
    axis: str = Field(
        default="",
        description=(
            "One sibling property; empty for route_existing/rehome_existing and normally "
            "add_sibling."
        ),
    )
    axis_question: str = Field(
        default="",
        description="One sibling question; empty when the existing boundary supplies it.",
    )
    moves: list[_PlanMove] = Field(min_length=1)


class _SubmitPlanArgs(BaseModel):
    boundaries: list[_BoundaryPlan] = Field(default_factory=list)


class _InitialBoundaryPlan(BaseModel):
    """Only plan shape available while a bounded scope is still flat."""

    parent: str = Field(default="", description="Flat folder whose first boundary is created.")
    operation: Literal["create_boundary"] = "create_boundary"
    axis: str = Field(description="One sibling property shared by every proposed class.")
    axis_question: str = Field(description="One direct question answered by every class name.")
    moves: list[_PlanMove] = Field(min_length=1)


class _SubmitInitialPlanArgs(BaseModel):
    boundaries: list[_InitialBoundaryPlan] = Field(min_length=1, max_length=1)


class _IncrementalBoundaryPlan(BaseModel):
    """Least-powerful operations for an already established bounded boundary."""

    parent: str = Field(default="", description="Existing boundary parent being extended.")
    operation: Literal["route_existing", "rehome_existing", "add_sibling"] = Field(
        description=(
            "route_existing files loose documents; rehome_existing repairs focused routing "
            "across the parent and existing children; add_sibling creates at least one new "
            "value. The host always inherits the existing axis and question."
        )
    )
    moves: list[_PlanMove] = Field(min_length=1)


class _SubmitIncrementalPlanArgs(BaseModel):
    boundaries: list[_IncrementalBoundaryPlan] = Field(min_length=1, max_length=1)


class _SemanticFinding(BaseModel):
    kind: Literal[
        "overlap",
        "contains_sibling",
        "level_mismatch",
        "mixed_axis",
        "catch_all",
        "over_partition",
        "duplicate_boundary",
        "family_split",
        "forced_fit",
        "insufficient_evidence",
    ]
    subjects: list[str] = Field(default_factory=list)
    evidence_handles: list[str] = Field(default_factory=list)
    instruction: str = Field(min_length=5)
    blocking: bool = True


class _SemanticReviewArgs(BaseModel):
    findings: list[_SemanticFinding] = Field(default_factory=list)


class _NoChangeArgs(BaseModel):
    reason: str = Field(
        min_length=10,
        description="Concrete reason the current structure should remain unchanged.",
    )


class _FinishExplorationArgs(BaseModel):
    summary: str = Field(
        min_length=10,
        description="Concise evidence summary for the fresh conclusion request.",
    )


def _document_handles(vault: Vault) -> dict[str, PurePosixPath]:
    documents = sorted(
        (
            path
            for path in vault.iter_files(PurePosixPath(), recursive=True)
            if not path.parts or path.parts[0] != INBOX.parts[0]
        ),
        key=lambda path: str(path).casefold(),
    )
    return {f"D{index:06d}": path for index, path in enumerate(documents, start=1)}


def _document_paths_by_id(vault: Vault) -> dict[str, PurePosixPath]:
    paths: dict[str, PurePosixPath] = {}
    for document in vault.iter_files(PurePosixPath(), recursive=True):
        sidecar = document.parent / sidecar_name(document.name)
        if not vault.exists(sidecar):
            continue
        meta = read_sidecar_meta(vault.read_text(sidecar)) or {}
        document_id = str(meta.get("document_id", ""))
        if document_id:
            paths[document_id] = document
    return paths


def family_handle_units(
    vault: Vault,
    *,
    handles: dict[str, PurePosixPath],
    document_ids: Sequence[str],
    catalog: Catalog | None,
) -> dict[str, tuple[str, ...]]:
    """Return the indivisible F-handles advertised for one movable window."""

    if catalog is None:
        return {}
    handle_by_path = {path: handle for handle, path in handles.items()}
    path_by_id = _document_paths_by_id(vault)
    units: dict[str, tuple[str, ...]] = {}
    family_number = 0
    for members in family_components(catalog, document_ids):
        member_handles = tuple(
            handle_by_path[path_by_id[document_id]]
            for document_id in members
            if document_id in path_by_id and path_by_id[document_id] in handle_by_path
        )
        if len(member_handles) < 2:
            continue
        family_number += 1
        units[f"F{family_number:03d}"] = member_handles
    return units


def _compact_card(
    vault: Vault,
    document: PurePosixPath,
    handle: str,
    *,
    catalog: Catalog | None = None,
) -> str:
    meta: dict[str, object] = {}
    sidecar = document.parent / sidecar_name(document.name)
    if vault.exists(sidecar):
        meta = read_sidecar_meta(vault.read_text(sidecar)) or {}
    document_id = str(meta.get("document_id", ""))
    card = catalog.load_card(document_id) if catalog is not None and document_id else None
    title = (card.title if card is not None else str(meta.get("title", ""))).strip()[:60]
    doc_type = (card.doc_type if card is not None else str(meta.get("doc_type", ""))).strip()[:20]
    topics = list(card.topics) if card is not None else meta.get("topics", [])
    topic_text = (
        ", ".join(str(item)[:20] for item in topics[:2]) if isinstance(topics, list) else ""
    )
    summary = " ".join(
        (card.summary if card is not None else str(meta.get("summary", ""))).split()
    )[:160]
    return (
        f"ID={handle} | AT={str(document.parent)[:60]} "
        f"| TITLE={title or document.stem[:60]} | TYPE={doc_type} "
        f"| TOPICS={topic_text} | SUMMARY={summary}"
    )


def _card_signals(vault: Vault, document: PurePosixPath) -> set[str]:
    """Language-neutral retrieval hints; never a classification verdict."""

    sidecar = document.parent / sidecar_name(document.name)
    meta = read_sidecar_meta(vault.read_text(sidecar)) if vault.exists(sidecar) else None
    if not meta:
        return set()
    values: list[str] = [str(meta.get("title", ""))]
    for key in ("topics", "keywords"):
        raw = meta.get(key, [])
        if isinstance(raw, list):
            values.extend(str(item) for item in raw)
    entities = meta.get("entities", [])
    if isinstance(entities, list):
        for entity in entities:
            if isinstance(entity, dict):
                values.extend(str(value) for value in entity.values() if value)
            else:
                values.append(str(entity))
    signals: set[str] = set()
    for value in values:
        normalised = " ".join(value.casefold().split())
        if len(normalised) >= 2:
            signals.add(normalised)
        signals.update(token for token in re.findall(r"[^\W_]{2,}", normalised) if len(token) >= 2)
    return signals


def build_arrivals_tool(
    vault: Vault,
    *,
    handles: dict[str, PurePosixPath],
    document_ids: Sequence[str],
    catalog: Catalog | None = None,
    family_units: dict[str, tuple[str, ...]] | None = None,
) -> Tool:
    """Expose one bounded window with indivisible F assignment handles."""
    handle_by_path = {path: handle for handle, path in handles.items()}
    path_by_id = _document_paths_by_id(vault)
    arrivals = [
        (document_id, path_by_id[document_id])
        for document_id in document_ids
        if document_id in path_by_id
    ]
    units = family_units or family_handle_units(
        vault,
        handles=handles,
        document_ids=document_ids,
        catalog=catalog,
    )
    family_by_handle = {
        member: (label, members) for label, members in units.items() for member in members
    }
    used = False

    async def _arrivals(_: _ArrivalsArgs) -> str:
        nonlocal used
        if used:
            return "Arrival window already read. Use the existing evidence and continue the plan."
        used = True
        rows = []
        for _document_id, document in arrivals:
            row = _compact_card(vault, document, handle_by_path.get(document, ""), catalog=catalog)
            if family := family_by_handle.get(handle_by_path.get(document, "")):
                label, member_handles = family
                row += (
                    f" | FAMILY_UNIT={label} | FAMILY_MEMBERS={','.join(member_handles)} "
                    f"| ASSIGN_WITH={label}"
                )
            rows.append(row)
        return "\n".join(rows) or "(no readable arrivals in this window)"

    return FunctionTool(
        name="arrivals",
        description=(
            "Read every compact card in the bounded new-arrival window that triggered "
            "this maintenance pass. FAMILY_UNIT rows are one assignment unit: submit the "
            "F handle, never its individual D members. Call once after tree."
        ),
        params=_ArrivalsArgs,
        handler=_arrivals,
        read_only=True,
        concurrency_safe=True,
    )


def build_read_tools(
    vault: Vault,
    charters: CharterService,
    *,
    handles: dict[str, PurePosixPath] | None = None,
    restrict_documents: bool = False,
    catalog: Catalog | None = None,
) -> list[Tool]:
    """The read-only tools that let an agent navigate the vault."""

    effective_handles = handles if handles is not None else _document_handles(vault)
    handle_by_path = {path: handle for handle, path in effective_handles.items()}

    def _folder(path: str) -> PurePosixPath:
        # Model-facing prompts conventionally spell the root and descendants as
        # `/` and `/folder`.  The vault port is deliberately relative.  Normalize
        # that one presentation slash here instead of turning a harmless read into a
        # path-escape error; `..` and real escapes are still rejected by the vault.
        if path in ("", "/", "."):
            return PurePosixPath()
        return PurePosixPath(path[1:] if path.startswith("/") else path)

    async def _ls(args: _LsArgs) -> str:
        folder = _folder(args.path)
        if not vault.is_dir(folder):
            return f"No such folder: {args.path or '/'}"
        depth = len(folder.parts)
        subfolders = [
            f.name
            for f in vault.iter_folders()
            if len(f.parts) == depth + 1 and f.parts[:depth] == folder.parts
        ]
        doc_lines: list[str] = []
        for f in vault.iter_files(folder):
            if restrict_documents and f not in handle_by_path:
                continue
            sidecar = f.parent / sidecar_name(f.name)
            doc_type = ""
            if vault.exists(sidecar):
                meta = read_sidecar_meta(vault.read_text(sidecar))
                if meta:
                    doc_type = str(meta.get("doc_type", ""))
            handle = handle_by_path.get(f, "")
            doc_lines.append(f"📄 {handle} {f.name}" + (f"  [{doc_type}]" if doc_type else ""))
        doc_lines.sort(key=str.casefold)
        window = doc_lines[args.offset : args.offset + args.limit]
        lines = [f"📁 {name}/" for name in sorted(subfolders)] + window
        if args.offset + args.limit < len(doc_lines):
            lines.append(
                f"… documents {args.offset + 1}-{args.offset + len(window)} of "
                f"{len(doc_lines)}; call ls again with offset={args.offset + args.limit}"
            )
        elif doc_lines:
            lines.append(f"(all {len(doc_lines)} direct documents shown)")
        return "\n".join(lines) or "(empty)"

    async def _tree(args: _TreeArgs) -> str:
        base = _folder(args.path)
        rows: list[str] = []
        for folder in vault.iter_folders():
            if not folder.parts or folder.parts[0] == INBOX.parts[0]:
                continue
            if base.parts and folder.parts[: len(base.parts)] != base.parts:
                continue
            indent = "  " * (len(folder.parts) - 1)
            rows.append(f"{indent}{folder.name}/  ({vault.count_files(folder)})")
        return "\n".join(rows) or "(no folders yet)"

    async def _inventory(args: _InventoryArgs) -> str:
        base = _folder(args.path)
        if not vault.is_dir(base):
            return f"No such folder: {args.path or '/'}"
        documents = sorted(
            (
                path
                for path in vault.iter_files(base, recursive=args.recursive)
                if path in handle_by_path
            ),
            key=lambda path: str(path).casefold(),
        )
        window = documents[args.offset : args.offset + args.limit]
        rows = [
            _compact_card(vault, document, handle_by_path.get(document, ""), catalog=catalog)
            for document in window
        ]
        end = args.offset + len(window)
        if end < len(documents):
            rows.append(
                f"… documents {args.offset + 1}-{end} of {len(documents)}; "
                f"call inventory again with offset={end}"
            )
        elif documents:
            rows.append(f"(all {len(documents)} documents shown)")
        return "\n".join(rows) or "(empty)"

    async def _read(args: _ReadArgs) -> str:
        if restrict_documents and args.path not in effective_handles:
            return f"Unknown or inaccessible document handle: {args.path}"
        target = effective_handles.get(args.path, PurePosixPath(args.path))
        if not vault.exists(target):
            return f"No such file: {args.path}"
        if target.suffix not in _TEXT_SUFFIXES:
            sidecar = target.parent / sidecar_name(target.name)
            if vault.exists(sidecar):
                target = sidecar
            else:
                return f"{args.path} has no readable text (no sidecar)."
        lines = vault.read_text(target).splitlines()
        window = lines[args.offset : args.offset + args.limit]
        numbered = "\n".join(f"{args.offset + i + 1}\t{line}" for i, line in enumerate(window))
        more = "" if args.offset + args.limit >= len(lines) else f"\n… ({len(lines)} lines total)"
        return numbered + more or "(empty file)"

    async def _grep(args: _GrepArgs) -> str:
        try:
            pattern = re.compile(args.pattern)
        except re.error as exc:
            return f"Invalid regex: {exc}"
        base = _folder(args.path)
        if not vault.is_dir(base):
            return f"No such folder: {args.path or '/'}"
        hits: list[str] = []
        for file in vault.iter_files(base, recursive=True):
            if restrict_documents and file not in handle_by_path:
                continue
            sidecar = file.parent / sidecar_name(file.name)
            target = (
                sidecar
                if vault.exists(sidecar)
                else (file if file.suffix in _TEXT_SUFFIXES else None)
            )
            if target is None:
                continue
            for lineno, line in enumerate(vault.read_text(target).splitlines(), start=1):
                if pattern.search(line):
                    hits.append(f"{file}:{lineno}: {line.strip()[:200]}")
                    if len(hits) >= _GREP_MATCH_LIMIT:
                        return "\n".join(hits) + f"\n… (stopped at {_GREP_MATCH_LIMIT} matches)"
        return "\n".join(hits) or "(no matches)"

    async def _read_note(args: _NoteArgs) -> str:
        folder = _folder(args.folder)
        note = folder / CHARTER_FILENAME
        if not vault.exists(note):
            return f"{args.folder or '/'} has no folder note."
        return vault.read_text(note)

    async def _related(args: _RelatedArgs) -> str:
        source = effective_handles.get(args.document)
        if source is None:
            return f"Unknown document handle: {args.document}"
        base = _folder(args.path)
        if not vault.is_dir(base):
            return f"No such folder: {args.path or '/'}"
        source_signals = _card_signals(vault, source)
        if not source_signals:
            return "(no reusable card signals)"
        ranked: list[tuple[int, str, PurePosixPath, list[str]]] = []
        for candidate in vault.iter_files(base, recursive=True):
            if candidate == source or candidate not in handle_by_path:
                continue
            shared = sorted(source_signals & _card_signals(vault, candidate))
            if shared:
                ranked.append((-len(shared), str(candidate).casefold(), candidate, shared[:6]))
        rows = [
            f"{handle_by_path[candidate]} AT={candidate.parent} SHARED={', '.join(shared)}"
            for _, _, candidate, shared in sorted(ranked)[: args.limit]
        ]
        return "\n".join(rows) or "(no related-card candidates)"

    return [
        FunctionTool(
            name="ls",
            description=(
                "List direct subfolders and one paginated window of direct documents. "
                "Follow the returned offset until all documents needed for a plan are inspected."
            ),
            params=_LsArgs,
            handler=_ls,
        ),
        FunctionTool(
            name="tree",
            description="Show the folder tree (with document counts) under a folder.",
            params=_TreeArgs,
            handler=_tree,
        ),
        FunctionTool(
            name="inventory",
            description=(
                "Read paginated compact cards for documents under a folder. Follow the "
                "returned offset so a plan is based on the complete relevant inventory."
            ),
            params=_InventoryArgs,
            handler=_inventory,
        ),
        FunctionTool(
            name="read",
            description=(
                "Read a document by its D/R handle, paginated. D handles are movable in the "
                "current window; R handles are reference-only and can never be submitted. Documents "
                "read their sidecars; use this for selected ambiguous inventory cards."
            ),
            params=_ReadArgs,
            handler=_read,
        ),
        FunctionTool(
            name="grep",
            description="Regex-search the text of documents' sidecars under a folder.",
            params=_GrepArgs,
            handler=_grep,
        ),
        FunctionTool(
            name="read_note",
            description="Read a folder's note describing what it holds.",
            params=_NoteArgs,
            handler=_read_note,
        ),
        FunctionTool(
            name="related",
            description=(
                "Surface possible document relatives from shared open card metadata. "
                "This is counterexample retrieval only; inspect results before judging family."
            ),
            params=_RelatedArgs,
            handler=_related,
        ),
    ]
