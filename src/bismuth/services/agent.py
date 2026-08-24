"""Agents over the vault, built on Bismuth's internal agent loop.

Two shapes, same tools underneath:
- ``ask``: read-only navigation (ls/tree/read/grep/note) to answer a question.
- ``organize``: the same plus an approval-gated ``move`` to reshape the folder tree.

Bismuth supplies the model, tools, and prompt to the loop.
"""

from __future__ import annotations

import bisect
import difflib
import itertools
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from pydantic import BaseModel, Field

from bismuth.agentkit import Agent, ChatModel, FunctionTool, RunResult, Tool, subagent_tool
from bismuth.agentkit.loop import OnEvent
from bismuth.domain.charter import CHARTER_FILENAME
from bismuth.domain.document import sidecar_name
from bismuth.ports.vault import INBOX, Vault
from bismuth.prompts.agent import (
    DEFAULT_ORGANIZE_INSTRUCTION,
    SYSTEM_ASK,
    SYSTEM_ORGANIZE,
    SYSTEM_VERIFIER,
)
from bismuth.services.charters import CharterService
from bismuth.services.sidecar import read_sidecar_meta

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
_GREP_FILE_LIMIT = 20
"""How many documents one grep names. Sized so the whole result fits in a tool result
without being clipped -- a result cut in the middle loses hits silently, while a tool
that stops on its own boundary can say what it left out."""
_GREP_HITS_PER_FILE = 5
_GREP_HITS_ALONE = 60
"""Hits shown when the search is aimed at one document rather than a folder. Asking about
one file is asking what is in it, and a sample cannot answer that."""
_GREP_LINE_CHARS = 200


_WHITESPACE = re.compile(r"\s+")


def _unwrapped(pattern: str) -> re.Pattern[str] | None:
    """The same pattern, to be matched against text with every space taken out.

    Extracted text may wrap phrases across lines. Anchored patterns are left unchanged
    because removing line boundaries would alter their meaning.
    """
    if "^" in pattern or "$" in pattern:
        return None
    try:
        return re.compile(_WHITESPACE.sub("", pattern))
    except re.error:
        return None


def _flatten(lines: list[str]) -> tuple[str, list[int]]:
    """All the non-space characters, and where each line ends in that string."""
    packed = ["".join(line.split()) for line in lines]
    return "".join(packed), list(itertools.accumulate(len(p) for p in packed))


def _matching_lines(
    text: str, pattern: re.Pattern[str], wrapped: re.Pattern[str] | None
) -> list[str]:
    """Lines that match, including where the match is split across a line ending."""
    lines = text.splitlines()
    hit: dict[int, str] = {}
    for number, line in enumerate(lines, start=1):
        if pattern.search(line):
            hit[number] = line
    # Build the line map only when whitespace-insensitive matching finds a hit.
    if wrapped is not None and wrapped.search(_WHITESPACE.sub("", text)):
        packed, ends = _flatten(lines)
        for match in wrapped.finditer(packed):
            number = bisect.bisect_right(ends, match.start()) + 1
            if number <= len(lines):
                hit.setdefault(number, lines[number - 1])
    return [f"  {number}: {hit[number].strip()[:_GREP_LINE_CHARS]}" for number in sorted(hit)]


class _LsArgs(BaseModel):
    path: str = Field(default="", description="Folder path, or empty for the vault root.")


class _TreeArgs(BaseModel):
    path: str = Field(default="", description="Folder to show under, or empty for the root.")


class _ReadArgs(BaseModel):
    path: str = Field(description="A file path. A document path reads its .md sidecar.")
    offset: int = Field(default=0, ge=0, description="First line to return (0-based).")
    limit: int = Field(default=200, ge=1, le=2000, description="How many lines to return.")


class _GrepArgs(BaseModel):
    pattern: str = Field(description="Regular expression to search for.")
    path: str = Field(
        default="",
        description="Folder or a single file to search under; empty searches the whole vault.",
    )


class _NoteArgs(BaseModel):
    folder: str = Field(default="", description="Folder whose note to read.")


class _MoveArgs(BaseModel):
    paths: list[str] = Field(description="Document paths to move.")
    target: str = Field(description="Destination folder path (created if it does not exist).")


class _RenameArgs(BaseModel):
    folder: str = Field(description="Folder to rename.")
    new_name: str = Field(description="New name -- a single segment, not a path.")


def build_read_tools(vault: Vault, charters: CharterService) -> list[Tool]:
    """The read-only tools that let an agent navigate the vault."""

    def _folder(path: str) -> PurePosixPath:
        return PurePosixPath(path) if path not in ("", "/") else PurePosixPath()

    def _missing(path: str) -> str:
        """Say a folder is not there, and name the ones it might have been meant for.

        A bare refusal leaves the caller guessing, and guessing again costs another
        turn. Names are matched on the last segment too, since a path is usually wrong
        in its parent rather than in the folder actually wanted.
        """
        wanted = PurePosixPath(path).name or path
        known = [str(f) for f in vault.iter_folders() if f.parts and f.parts[0] != INBOX.parts[0]]
        near = [k for k in known if PurePosixPath(k).name == wanted]
        near += [
            k for k in difflib.get_close_matches(path, known, n=3, cutoff=0.5) if k not in near
        ]
        if not near:
            return f"No such folder: {path or '/'}. Use `tree` to see the real paths."
        return f"No such folder: {path or '/'}. Did you mean: " + ", ".join(near[:3])

    async def _ls(args: _LsArgs) -> str:
        folder = _folder(args.path)
        if not vault.is_dir(folder):
            return _missing(args.path)
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
            doc_lines.append(f"📄 {f.name}" + (f"  [{doc_type}]" if doc_type else ""))
        lines = [f"📁 {name}/" for name in sorted(subfolders)] + doc_lines
        return "\n".join(lines) or "(empty)"

    async def _tree(args: _TreeArgs) -> str:
        """Return one usable folder path per line."""
        base = _folder(args.path)
        rows: list[str] = []
        for folder in vault.iter_folders():
            if not folder.parts or folder.parts[0] == INBOX.parts[0]:
                continue
            if base.parts and folder.parts[: len(base.parts)] != base.parts:
                continue
            rows.append(f"{folder}/  ({vault.count_files(folder)})")
        return "\n".join(rows) or "(no folders yet)"

    async def _read(args: _ReadArgs) -> str:
        target = PurePosixPath(args.path)
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
        if vault.is_dir(base):
            files = list(vault.iter_files(base, recursive=True))
            per_file = _GREP_HITS_PER_FILE
        elif vault.exists(base):
            # A single-file search returns every matching line.
            files, per_file = [base], _GREP_HITS_ALONE
        else:
            return _missing(args.path)
        wrapped = _unwrapped(args.pattern)
        found: list[tuple[PurePosixPath, list[str]]] = []
        for file in files:
            sidecar = file.parent / sidecar_name(file.name)
            target = (
                sidecar
                if vault.exists(sidecar)
                else (file if file.suffix in _TEXT_SUFFIXES else None)
            )
            if target is None:
                continue
            lines = _matching_lines(vault.read_text(target), pattern, wrapped)
            if lines:
                found.append((file, lines))
            if len(found) > _GREP_FILE_LIMIT:
                break
        if not found:
            return "(no matches)"

        # Group hits by file to keep paths and passages readable.
        rows: list[str] = []
        for file, lines in found[:_GREP_FILE_LIMIT]:
            rows.append(str(file))
            rows.extend(lines[:per_file])
            if len(lines) > per_file:
                rows.append(f"  … {len(lines)} matches in this document")
        if len(found) > _GREP_FILE_LIMIT:
            rows.append("… More documents match. Narrow the folder or search expression.")
        return "\n".join(rows)

    async def _read_note(args: _NoteArgs) -> str:
        folder = _folder(args.folder)
        note = folder / CHARTER_FILENAME
        if not vault.exists(note):
            if not vault.is_dir(folder):
                return _missing(args.folder)
            return f"{args.folder or '/'} has no folder note."
        return vault.read_text(note)

    return [
        FunctionTool(
            name="ls",
            description="List the folders and files directly in a folder.",
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
            name="read",
            description="Read a file's text (a document reads its sidecar), paginated.",
            params=_ReadArgs,
            handler=_read,
        ),
        FunctionTool(
            name="grep",
            description=(
                "Regex-search document text. Give a folder to search everything under it, "
                "or one file to find where in that document a thing is said."
            ),
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
    """One move the organizer would make, pending the user's approval."""

    paths: list[str]
    target: str


@dataclass(frozen=True, slots=True)
class ProposedRename:
    """A folder rename the organizer would make, pending the user's approval."""

    folder: str
    new_name: str


@dataclass(frozen=True, slots=True)
class ReorgProposal:
    """A reorganisation plan: moves + renames to apply, and the agent's explanation."""

    moves: list[ProposedMove]
    renames: list[ProposedRename]
    summary: str


def build_propose_move_tool(sink: list[ProposedMove]) -> Tool:
    """A read-only tool that RECORDS an intended move. Nothing moves until the user approves."""

    async def _propose(args: _MoveArgs) -> str:
        sink.append(ProposedMove(paths=list(args.paths), target=args.target))
        return (
            f"Proposed: move {len(args.paths)} document(s) into {args.target}/ "
            f"(applied only after the user approves)."
        )

    return FunctionTool(
        name="move",
        description=(
            "Propose moving documents into a folder to reorganise the tree. "
            "Applied only after the user approves your whole plan."
        ),
        params=_MoveArgs,
        handler=_propose,
        read_only=True,
    )


def build_rename_tool(sink: list[ProposedRename]) -> Tool:
    """A read-only tool that RECORDS an intended folder rename, applied after approval."""

    async def _propose(args: _RenameArgs) -> str:
        sink.append(ProposedRename(folder=args.folder, new_name=args.new_name))
        return f"Proposed: rename '{args.folder}' to '{args.new_name}' (applied after approval)."

    return FunctionTool(
        name="rename",
        description=(
            "Propose renaming a folder whose name no longer describes its contents "
            "(when the grouping is fine but the label is wrong). Applied after approval."
        ),
        params=_RenameArgs,
        handler=_propose,
        read_only=True,
    )


class AgentService:
    """Runs vault agents: read-only Q&A, and structure reorganisation proposals."""

    def __init__(self, *, model: ChatModel, vault: Vault, charters: CharterService) -> None:
        self._model = model
        self._vault = vault
        self._charters = charters

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
        on_event: OnEvent | None = None,
    ) -> ReorgProposal:
        """Inspect the vault and return a reorganisation plan. Proposes only; never moves."""
        moves: list[ProposedMove] = []
        renames: list[ProposedRename] = []
        verifier = Agent(
            model=self._model,
            tools=build_read_tools(self._vault, self._charters),
            system=SYSTEM_VERIFIER,
        )
        tools = [
            *build_read_tools(self._vault, self._charters),
            build_propose_move_tool(moves),
            build_rename_tool(renames),
            subagent_tool({"verifier": verifier}, on_event=on_event),
        ]
        agent = Agent(model=self._model, tools=tools, system=SYSTEM_ORGANIZE, on_event=on_event)
        result = await agent.run(instruction)
        return ReorgProposal(moves=moves, renames=renames, summary=result.text)
