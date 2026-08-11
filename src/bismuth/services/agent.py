"""Tool-using agents over the vault, including the autonomous librarian planner.

The organizer never mutates while it is reasoning.  It submits one complete shadow
plan, ordinary code validates and simulates that plan, and only then is the entire
change journalled and applied as one transaction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from agentkit import Agent, ChatModel, FunctionTool, RunResult, Tool, subagent_tool
from agentkit.loop import OnEvent
from pydantic import BaseModel, Field

from bismuth.domain.charter import CHARTER_FILENAME, Charter, boundary_purpose
from bismuth.domain.document import sidecar_name
from bismuth.domain.journal import Actor, JournalEntry, Operation, OperationKind
from bismuth.domain.paths import sanitize_segment
from bismuth.logging_setup import log_trace
from bismuth.ports.vault import INBOX, Vault
from bismuth.services.charters import CharterService
from bismuth.services.sidecar import read_sidecar_meta
from bismuth.services.transactor import Transactor

DEFAULT_ORGANIZE_INSTRUCTION = (
    "Review the vault's structure and propose any reorganisation it needs."
)

SYSTEM_ASK = """\
You are a librarian answering questions from a vault of real folders and files.

Every document has a greppable Markdown sidecar next to it (``<name>.md``) holding \
its extracted text and a header (title, topics, entities, summary). Every folder \
has a ``_folder.md`` note describing what it holds.

Work by navigating: `tree` to see the shape, `read_note` to learn what a folder is \
for, `grep` to find where something is said, `read` to read a document's sidecar. \
Prefer grep/read_note over reading every file. When you answer, cite the folders \
and files you used. If the vault does not contain the answer, say so plainly.\
"""

SYSTEM_ORGANIZE = """\
You are the planning half of an AI librarian. You may inspect a real document vault, \
but you can only submit a SHADOW PLAN. You never mutate files yourself.

Maintain one coherent mental model throughout the run:
1. Start with `tree`, then inspect the requested scope with paginated `inventory`, folder \
notes, and selected document sidecars. Follow every inventory page needed by the plan; \
do not infer a whole collection from one page or from folder names alone.
2. Identify a navigation problem before designing a taxonomy. A large uniform folder \
is not automatically a problem.
3. For each parent you reorganise, choose ONE property and create at least TWO sibling \
classes that are direct, mutually exclusive answers to one question.
4. A class name is one reusable value, not a comparison (never "A vs B"), a sentence, \
a current path, a filename, a list of titles, or text containing an extension such as \
`.pdf`. Use the documents' own language.
5. Preserve documents that do not confidently fit; omitting them from moves is safer \
than inventing a remainder folder. Reuse a suitable existing child where possible.
Use only the deterministic `D000001` handles shown by `inventory` when assigning \
documents; never copy a document path into the submitted membership list.
6. Ask the `verifier` sub-agent to challenge the COMPLETE intended plan, including its \
axis, names, and representative document paths. Revise it if challenged.
7. Call `submit_plan` with the final complete plan. If validation rejects it, use the \
returned problems to revise and submit a complete replacement. If no coherent \
improvement survives inspection, submit nothing and explain why.

Do not call individual move or rename tools. The application validates the complete \
shadow plan as one object and rejects path leakage, missing files, duplicate membership, \
one-class partitions, singleton new shelves, and names that would need sanitising. A \
validated plan may be applied automatically and atomically, so omit uncertain moves.\
"""

SYSTEM_VERIFIER = """\
You are a hostile reviewer of a proposed library shadow plan. Inspect the vault rather \
than trusting the proposal. Try to disprove that every sibling name is one direct value \
of the stated axis, that representative documents belong, and that the change improves \
navigation enough to justify churn. Explicitly reject comparisons, filename/path copies, \
mixed axes, title shelves, forced coverage, and plans inferred from an incomplete listing. \
Return concrete keep/drop/revise findings; you never apply changes.\
"""

_TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".markdown",
    ".rst",
    ".log",
    ".csv",
    ".tsv",
    ".json",
    ".yaml",
    ".yml",
}
_GREP_MATCH_LIMIT = 100


class _LsArgs(BaseModel):
    path: str = Field(default="", description="Folder path, or empty for the vault root.")
    offset: int = Field(default=0, ge=0, description="First document to return (0-based).")
    limit: int = Field(default=50, ge=1, le=100, description="Documents to return per page.")


class _TreeArgs(BaseModel):
    path: str = Field(default="", description="Folder to show under, or empty for the root.")


class _InventoryArgs(BaseModel):
    path: str = Field(default="", description="Folder to inventory, or empty for the root.")
    recursive: bool = Field(default=True, description="Include documents in descendants.")
    offset: int = Field(default=0, ge=0, description="First document to return (0-based).")
    limit: int = Field(default=25, ge=1, le=50, description="Document cards per page.")


class _ReadArgs(BaseModel):
    path: str = Field(description="A D-prefixed document handle or file path.")
    offset: int = Field(default=0, ge=0, description="First line to return (0-based).")
    limit: int = Field(default=200, ge=1, le=2000, description="How many lines to return.")


class _GrepArgs(BaseModel):
    pattern: str = Field(description="Regular expression to search for.")
    path: str = Field(default="", description="Folder to search under, or empty for the root.")


class _NoteArgs(BaseModel):
    folder: str = Field(default="", description="Folder whose note to read.")


class _PlanMove(BaseModel):
    document_ids: list[str] = Field(
        min_length=1, description="D-prefixed document handles shown by inventory."
    )
    target: str = Field(description="One direct child of the boundary parent.")


class _BoundaryPlan(BaseModel):
    parent: str = Field(default="", description="Existing folder whose direct boundary changes.")
    axis: str = Field(description="One property shared by every proposed sibling.")
    axis_question: str = Field(description="One question every sibling name directly answers.")
    moves: list[_PlanMove] = Field(min_length=2)


class _SubmitPlanArgs(BaseModel):
    boundaries: list[_BoundaryPlan] = Field(default_factory=list)


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


def build_read_tools(
    vault: Vault,
    charters: CharterService,
    *,
    handles: dict[str, PurePosixPath] | None = None,
) -> list[Tool]:
    """The read-only tools that let an agent navigate the vault."""

    effective_handles = handles or _document_handles(vault)
    handle_by_path = {path: handle for handle, path in effective_handles.items()}

    def _folder(path: str) -> PurePosixPath:
        return PurePosixPath(path) if path not in ("", "/") else PurePosixPath()

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
        rows: list[str] = []
        for document in window:
            meta: dict[str, object] = {}
            sidecar = document.parent / sidecar_name(document.name)
            if vault.exists(sidecar):
                meta = read_sidecar_meta(vault.read_text(sidecar)) or {}
            title = str(meta.get("title", "")).strip()[:60]
            doc_type = str(meta.get("doc_type", "")).strip()[:20]
            topics = meta.get("topics", [])
            topic_text = (
                ", ".join(str(item)[:20] for item in topics[:2]) if isinstance(topics, list) else ""
            )
            summary = " ".join(str(meta.get("summary", "")).split())[:60]
            rows.append(
                f"ID={handle_by_path.get(document, '')} | AT={str(document.parent)[:60]} "
                f"| TITLE={title or document.stem[:60]} | TYPE={doc_type} "
                f"| TOPICS={topic_text} | SUMMARY={summary}"
            )
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
                "Read a document by its D-handle (or a file path), paginated. Documents "
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
    ]


@dataclass(frozen=True, slots=True)
class ProposedMove:
    """One validated move in a complete shadow plan."""

    paths: list[str]
    target: str


@dataclass(frozen=True, slots=True)
class ProposedRename:
    """Legacy manual-organizer shape; autonomous plans do not rename folders."""

    folder: str
    new_name: str


@dataclass(frozen=True, slots=True)
class ProposedBoundary:
    """One parent boundary and all sibling moves proposed beneath it."""

    parent: str
    axis: str
    axis_question: str
    moves: list[ProposedMove]


@dataclass(frozen=True, slots=True)
class ReorgProposal:
    """A validated, still-unapplied shadow plan and the agent's explanation."""

    moves: list[ProposedMove]
    renames: list[ProposedRename]
    summary: str
    boundaries: list[ProposedBoundary]
    problems: list[str]


@dataclass(frozen=True, slots=True)
class ReorgResult:
    """Outcome of an autonomous plan/validate/apply cycle."""

    proposal: ReorgProposal
    applied: bool
    moved: int = 0


def _folder(raw: str) -> PurePosixPath:
    return PurePosixPath(raw) if raw not in ("", "/") else PurePosixPath()


def _within(path: PurePosixPath, parent: PurePosixPath) -> bool:
    return path.parts[: len(parent.parts)] == parent.parts


def _strict_folder(raw: str) -> PurePosixPath | None:
    """Accept a model path only when sanitising would leave every segment unchanged."""
    if "\\" in raw or raw.startswith("/") or raw.endswith("/"):
        return None
    parts = raw.split("/") if raw else []
    if not parts or any(not part or part in (".", "..") or part != part.strip() for part in parts):
        return None
    try:
        if any(sanitize_segment(part) != part for part in parts):
            return None
    except ValueError:
        return None
    return PurePosixPath(*parts)


def _validate_shadow_plan(
    vault: Vault,
    args: _SubmitPlanArgs,
    *,
    scope: PurePosixPath,
    handles: dict[str, PurePosixPath],
) -> tuple[list[ProposedBoundary], list[str]]:
    """Validate and simulate a complete plan without changing the vault."""
    problems: list[str] = []
    accepted: list[ProposedBoundary] = []
    assigned: set[PurePosixPath] = set()
    boundary_parents: set[PurePosixPath] = set()
    planned_destinations: set[str] = set()

    if not args.boundaries:
        return [], ["the submitted plan has no boundaries"]

    for boundary in args.boundaries:
        parent = (
            PurePosixPath() if boundary.parent in ("", "/") else _strict_folder(boundary.parent)
        )
        if parent is None:
            problems.append(f"invalid boundary parent: {boundary.parent!r}")
            continue
        if parent in boundary_parents:
            problems.append(f"{parent or '/'} is submitted more than once")
            continue
        boundary_parents.add(parent)
        if not vault.is_dir(parent):
            problems.append(f"boundary parent does not exist: {parent or '/'}")
            continue
        if not _within(parent, scope):
            problems.append(f"boundary is outside requested scope: {parent or '/'}")
            continue
        available = set(vault.iter_files(parent, recursive=True))
        document_stems = {path.stem.casefold() for path in available}
        document_suffixes = {path.suffix.casefold() for path in available if path.suffix}
        axis = " ".join(boundary.axis.split()).strip()
        question = " ".join(boundary.axis_question.split()).strip()
        if not axis or "\n" in boundary.axis or "\r" in boundary.axis:
            problems.append(f"{parent or '/'} has an invalid axis")
        if not question or "\n" in boundary.axis_question or "\r" in boundary.axis_question:
            problems.append(f"{parent or '/'} has an invalid axis question")

        targets: dict[PurePosixPath, list[PurePosixPath]] = {}
        for move in boundary.moves:
            target = _strict_folder(move.target)
            if target is None:
                problems.append(f"target would be altered by path sanitisation: {move.target!r}")
                continue
            if target.parent != parent:
                problems.append(f"target is not a direct child of {parent or '/'}: {target}")
                continue
            if target.parts and target.parts[0] == INBOX.parts[0]:
                problems.append(f"target is inside the inbox: {target}")
                continue
            if vault.exists(target) and not vault.is_dir(target):
                problems.append(f"target is an existing file: {target}")
                continue
            if target.name.casefold() == axis.casefold():
                problems.append(f"class name repeats its axis: {target.name}")
            if re.search(r"(?i)(?:\bvs\.?\b|\bversus\b|↔)", target.name):
                problems.append(f"class name is a comparison, not one axis value: {target.name}")
            if target.name.casefold() in document_stems or any(
                target.name.casefold().endswith(suffix) for suffix in document_suffixes
            ):
                problems.append(f"class name copies a document filename: {target.name}")
            bucket = targets.setdefault(target, [])
            for document_id in move.document_ids:
                source = handles.get(document_id)
                if source is None:
                    problems.append(f"unknown document handle: {document_id}")
                    continue
                if source in assigned:
                    problems.append(f"document is assigned more than once: {source}")
                    continue
                if source not in available:
                    problems.append(f"unknown document: {source}")
                    continue
                if not _within(source, parent) or not _within(source, scope):
                    problems.append(f"document is outside its boundary: {source}")
                    continue
                destination = target / source.name
                destination_key = str(destination).casefold()
                if destination_key in planned_destinations:
                    problems.append(f"two documents would collide at {destination}")
                    continue
                if destination != source and vault.exists(destination):
                    problems.append(f"destination already contains {source.name}: {target}")
                    continue
                assigned.add(source)
                planned_destinations.add(destination_key)
                bucket.append(source)

        nonempty = {target: paths for target, paths in targets.items() if paths}
        if len(nonempty) < 2:
            problems.append(f"{parent or '/'} does not create at least two sibling classes")
        for target, paths in nonempty.items():
            if not vault.exists(target) and len(paths) < 2:
                problems.append(f"new shelf {target} would contain only one document")
        accepted.append(
            ProposedBoundary(
                parent=str(parent),
                axis=axis,
                axis_question=question,
                moves=[
                    ProposedMove(paths=[str(path) for path in paths], target=str(target))
                    for target, paths in nonempty.items()
                ],
            )
        )

    return accepted, list(dict.fromkeys(problems))


def build_submit_plan_tool(
    vault: Vault,
    *,
    scope: PurePosixPath,
    handles: dict[str, PurePosixPath],
    sink: list[ProposedBoundary],
    problem_sink: list[str],
) -> Tool:
    """Capture only a complete plan that passes deterministic shadow validation."""

    async def _submit(args: _SubmitPlanArgs) -> str:
        boundaries, problems = _validate_shadow_plan(vault, args, scope=scope, handles=handles)
        if problems:
            sink.clear()
            problem_sink[:] = problems
            return "Plan rejected; submit a complete replacement after fixing:\n- " + "\n- ".join(
                problems
            )
        sink[:] = boundaries
        problem_sink.clear()
        move_count = sum(len(move.paths) for b in boundaries for move in b.moves)
        return f"Shadow plan accepted: {len(boundaries)} boundaries, {move_count} document moves."

    return FunctionTool(
        name="submit_plan",
        description=(
            "Submit one complete shadow plan. This validates paths, membership, sibling "
            "boundaries, collisions, and a simulated result; it does not mutate the vault."
        ),
        params=_SubmitPlanArgs,
        handler=_submit,
        read_only=True,
        concurrency_safe=False,
    )


class AgentService:
    """Runs Q&A plus shadow-planned, transactionally applied maintenance."""

    def __init__(
        self,
        *,
        model: ChatModel,
        vault: Vault,
        charters: CharterService,
        transactor: Transactor | None = None,
    ) -> None:
        self._model = model
        self._vault = vault
        self._charters = charters
        self._transactor = transactor

    async def ask(self, question: str, *, on_event: OnEvent | None = None) -> RunResult:
        agent = Agent(
            model=self._model,
            tools=build_read_tools(self._vault, self._charters),
            system=SYSTEM_ASK,
            on_event=on_event,
        )
        return await agent.run(question)

    async def propose_reorg(
        self,
        instruction: str = DEFAULT_ORGANIZE_INSTRUCTION,
        *,
        scope: str = "",
        on_event: OnEvent | None = None,
    ) -> ReorgProposal:
        """Inspect the vault and return one validated shadow plan. Never mutates."""
        scope_path = PurePosixPath() if scope in ("", "/") else _strict_folder(scope)
        if scope_path is None:
            return ReorgProposal([], [], f"Invalid scope: {scope}", [], ["invalid scope"])
        if not self._vault.is_dir(scope_path):
            return ReorgProposal([], [], f"No such scope: {scope or '/'}", [], ["missing scope"])
        handles = _document_handles(self._vault)
        boundaries: list[ProposedBoundary] = []
        problems: list[str] = []
        verifier = Agent(
            model=self._model,
            tools=build_read_tools(self._vault, self._charters, handles=handles),
            system=SYSTEM_VERIFIER,
        )
        tools = [
            *build_read_tools(self._vault, self._charters, handles=handles),
            build_submit_plan_tool(
                self._vault,
                scope=scope_path,
                handles=handles,
                sink=boundaries,
                problem_sink=problems,
            ),
            subagent_tool({"verifier": verifier}, on_event=on_event),
        ]
        agent = Agent(
            model=self._model,
            tools=tools,
            system=SYSTEM_ORGANIZE,
            max_turns=24,
            on_event=on_event,
        )
        result = await agent.run(instruction)
        moves = [move for boundary in boundaries for move in boundary.moves]
        return ReorgProposal(
            moves=moves,
            renames=[],
            summary=result.text,
            boundaries=boundaries,
            problems=problems,
        )

    async def reorganize(
        self,
        instruction: str = DEFAULT_ORGANIZE_INSTRUCTION,
        *,
        scope: str = "",
        on_event: OnEvent | None = None,
    ) -> ReorgResult:
        """Plan against a snapshot and atomically apply only a still-valid plan."""
        scope_path = PurePosixPath() if scope in ("", "/") else _strict_folder(scope)
        if scope_path is None or not self._vault.is_dir(scope_path):
            proposal = ReorgProposal(
                moves=[],
                renames=[],
                summary=f"Invalid or missing scope: {scope}",
                boundaries=[],
                problems=["invalid scope"],
            )
            return ReorgResult(proposal=proposal, applied=False)
        if self._vault.count_files(scope_path, recursive=True) < 4:
            proposal = ReorgProposal(
                moves=[],
                renames=[],
                summary="Fewer than four documents cannot form two reusable shelves.",
                boundaries=[],
                problems=[],
            )
            return ReorgResult(proposal=proposal, applied=False)
        proposal = await self.propose_reorg(
            instruction,
            scope=scope,
            on_event=on_event,
        )
        if not proposal.boundaries:
            log_trace(
                "agent_maintenance.skipped",
                scope=scope or "/",
                problems=proposal.problems,
                summary=proposal.summary,
            )
            return ReorgResult(proposal=proposal, applied=False)

        # Rebuild the typed submission and validate again immediately before execution.
        # The first validation happened during the tool loop; this closes the window in
        # which another filesystem actor could invalidate the snapshot.
        current_handles = _document_handles(self._vault)
        handle_by_path = {path: handle for handle, path in current_handles.items()}
        submitted = _SubmitPlanArgs(
            boundaries=[
                _BoundaryPlan(
                    parent=boundary.parent,
                    axis=boundary.axis,
                    axis_question=boundary.axis_question,
                    moves=[
                        _PlanMove(
                            document_ids=[
                                handle_by_path.get(PurePosixPath(path), "MISSING")
                                for path in move.paths
                            ],
                            target=move.target,
                        )
                        for move in boundary.moves
                    ],
                )
                for boundary in proposal.boundaries
            ]
        )
        boundaries, problems = _validate_shadow_plan(
            self._vault,
            submitted,
            scope=scope_path,
            handles=current_handles,
        )
        if problems:
            rejected = ReorgProposal(
                moves=[],
                renames=[],
                summary=proposal.summary,
                boundaries=[],
                problems=problems,
            )
            log_trace(
                "agent_maintenance.rejected",
                scope=scope or "/",
                problems=problems,
            )
            return ReorgResult(proposal=rejected, applied=False)

        moved = self._apply_boundaries(boundaries)
        applied = moved > 0
        log_trace(
            "agent_maintenance.applied" if applied else "agent_maintenance.skipped",
            scope=scope or "/",
            boundaries=len(boundaries),
            moved=moved,
            summary=proposal.summary,
        )
        return ReorgResult(proposal=proposal, applied=applied, moved=moved)

    def _apply_boundaries(self, boundaries: list[ProposedBoundary]) -> int:
        """Compile an accepted shadow plan into one journal transaction."""
        if self._transactor is None:
            raise RuntimeError("autonomous maintenance requires a transactor")
        operations: list[Operation] = []
        payloads: dict[PurePosixPath, bytes] = {}
        made: set[PurePosixPath] = set()
        signed: set[PurePosixPath] = set()
        moved_from: set[PurePosixPath] = set()
        moved_to: set[PurePosixPath] = set()
        moved = 0

        for boundary in boundaries:
            parent = _folder(boundary.parent)
            subtree_count = self._vault.count_files(parent, recursive=True)
            if parent.parts and self._charters.is_managed(parent):
                current = self._charters.load(parent)
                parent_charter = Charter(
                    path=parent,
                    title=current.title if current is not None else parent.name,
                    purpose=(
                        current.purpose
                        if current is not None
                        else boundary_purpose(boundary.axis, parent.name)
                    ),
                    managed=True,
                    split_basis=boundary.axis,
                    split_question=boundary.axis_question,
                    split_at_documents=subtree_count,
                )
                operation, payload = self._charters.write_operation(parent_charter)
                operations.append(operation)
                payloads[operation.target] = payload

            for move in boundary.moves:
                target = PurePosixPath(move.target)
                if not self._vault.exists(target) and target not in made:
                    operations.append(Operation(kind=OperationKind.MKDIR, target=target))
                    made.add(target)
                note = target / CHARTER_FILENAME
                if (
                    target not in signed
                    and not self._vault.exists(note)
                    and self._charters.is_managed(target)
                ):
                    signed.add(target)
                    target_charter = Charter(
                        path=target,
                        title=target.name,
                        purpose=boundary_purpose(boundary.axis, target.name),
                        managed=True,
                    )
                    note_op, note_payload = self._charters.write_operation(target_charter)
                    operations.append(note_op)
                    payloads[note_op.target] = note_payload
                for raw_source in move.paths:
                    source = PurePosixPath(raw_source)
                    if source.parent == target:
                        continue
                    destination = target / source.name
                    moved_from.add(source)
                    moved_to.add(destination)
                    operations.append(
                        Operation(
                            kind=OperationKind.MOVE,
                            source=source,
                            target=destination,
                            note=f"agent boundary: {boundary.axis}",
                        )
                    )
                    sidecar = source.parent / sidecar_name(source.name)
                    if self._vault.exists(sidecar):
                        operations.append(
                            Operation(
                                kind=OperationKind.MOVE,
                                source=sidecar,
                                target=target / sidecar_name(destination.name),
                                note="document sidecar",
                            )
                        )
                    moved += 1

        if not moved:
            return 0
        operations.extend(self._retirement_operations(boundaries, moved_from, moved_to))
        self._transactor.execute(
            JournalEntry(
                actor=Actor.BISMUTH,
                reason=f"agent maintenance: {moved} document(s)",
                operations=tuple(operations),
            ),
            payloads=payloads,
        )
        return moved

    def _retirement_operations(
        self,
        boundaries: list[ProposedBoundary],
        moved_from: set[PurePosixPath],
        moved_to: set[PurePosixPath],
    ) -> list[Operation]:
        """Retire only managed folders proven empty in the simulated final tree."""
        final_documents = set(self._vault.iter_files(PurePosixPath(), recursive=True))
        final_documents.difference_update(moved_from)
        final_documents.update(moved_to)
        parents = {_folder(boundary.parent) for boundary in boundaries}
        folders = list(self._vault.iter_folders())
        candidates: set[PurePosixPath] = set()
        for folder in folders:
            if not folder.parts or folder.parts[0] == INBOX.parts[0]:
                continue
            if not any(_within(folder, parent) for parent in parents):
                continue
            if folder in parents or any(_within(document, folder) for document in final_documents):
                continue
            charter = self._charters.load(folder)
            if charter is None or not charter.managed:
                continue
            candidates.add(folder)

        # An RMDIR is intentionally non-recursive.  Preserve an ancestor's note too
        # when any descendant is human-owned or otherwise not eligible for retirement.
        retire = [
            folder
            for folder in candidates
            if all(
                other in candidates
                for other in folders
                if other != folder and _within(other, folder)
            )
        ]

        operations: list[Operation] = []
        for folder in sorted(retire, key=lambda item: len(item.parts), reverse=True):
            note = folder / CHARTER_FILENAME
            if self._vault.exists(note):
                operations.append(
                    Operation(kind=OperationKind.REMOVE, target=note, note="retire empty sign")
                )
            operations.append(
                Operation(kind=OperationKind.RMDIR, target=folder, note="retire empty shelf")
            )
        return operations
