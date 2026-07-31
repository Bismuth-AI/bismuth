"""Agents over the vault, built on the standalone ``agentkit`` library.

Two shapes, same tools underneath:
- ``ask``: read-only navigation (ls/tree/read/grep/note) to answer a question.
- ``organize``: the same plus an approval-gated ``move`` to reshape the folder tree.

bismuth supplies the model, the tools (thin wrappers over the vault/services), and
the prompt; agentkit runs the loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from pydantic import BaseModel, Field

from agentkit import Agent, ChatModel, FunctionTool, RunResult, Tool, subagent_tool
from agentkit.loop import OnEvent
from bismuth.domain.charter import CHARTER_FILENAME
from bismuth.domain.document import sidecar_name
from bismuth.ports.vault import INBOX, Vault
from bismuth.services.charters import CharterService
from bismuth.services.sidecar import read_sidecar_meta

DEFAULT_ORGANIZE_INSTRUCTION = "Review the vault's structure and propose any reorganisation it needs."

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
You are an archivist keeping a document vault well organised, so an agent (or a \
person) can navigate it. Real folders, real files; each document has a `.md` \
sidecar with its text, each folder a `_folder.md` note.

FIRST look, THEN judge, THEN act:
1. Use `tree`, `read_note`, `grep`, and `read` to understand what is actually here \
-- what each folder holds and how it is (or isn't) organised. Do not decide from \
folder names alone.
2. Judge whether the structure genuinely needs work. A folder is fine if it is \
navigable -- even a large one, if its contents are uniform. Only act where a person \
would struggle: a pile of unlike documents at one level, near-duplicate folders for \
one idea, or a folder whose NAME no longer describes what is inside.
   Do NOT trust a folder's `_folder.md` note to decide it is fine -- the note is \
regenerated to fit whatever the folder currently holds, so it always seems to \
match. Judge from the documents' ACTUAL types (shown in `ls` as `[type]`) and the \
folder's name. A folder named e.g. "사업추진현황 보고" that in fact holds financial \
statements, audit reports, and board minutes has a name that no longer fits and \
several distinct types piled together -- that wants splitting or renaming.
3. When you act, choose the lighter fix:
   - If the grouping is fine but the folder's NAME no longer fits its contents, \
`rename` the folder (e.g. a folder called "사업추진현황 보고" that holds many kinds of \
project documents could become "라자스탄 태양광 문서" or similar). Do not split what \
does not need splitting.
   - If genuinely different things are piled together, PROPOSE `move`s: group \
documents into subfolders by a distinction a person would browse by -- document \
type, period, sub-topic -- in the documents' own language. Reuse the right existing \
branch; do not invent a parallel one. Move the EXISTING documents, not just future \
ones.
Nothing is applied until the user approves your whole plan, so propose every move \
and rename you would make.

Before finalising a non-trivial plan, delegate it to the `verifier` sub-agent \
(via `task`) to catch churn or mistakes, and drop whatever it rejects.

There is no size rule -- judge by whether the structure helps someone find things. \
If it is already good, say so and propose nothing. End with a short summary of the \
plan (or why nothing needs changing).\
"""

SYSTEM_VERIFIER = """\
You review a proposed folder reorganisation before a person sees it. You are given \
the plan (which documents move where) and can inspect the vault with the read \
tools. Judge honestly: does the plan make the vault easier to navigate, or is it \
churn or a mistake -- splitting a folder that was already fine, wrong groupings, \
names that do not match the documents' language? Reply with a short verdict for \
each part: keep, drop, or reject, with one reason each.\
"""

_TEXT_SUFFIXES = {".md", ".txt", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml"}
_GREP_MATCH_LIMIT = 100


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
    path: str = Field(default="", description="Folder to search under, or empty for the root.")


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
            doc_lines.append(f"📄 {f.name}" + (f"  [{doc_type}]" if doc_type else ""))
        lines = [f"📁 {name}/" for name in sorted(subfolders)] + doc_lines
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
        FunctionTool(name="ls", description="List the folders and files directly in a folder.", params=_LsArgs, handler=_ls),
        FunctionTool(name="tree", description="Show the folder tree (with document counts) under a folder.", params=_TreeArgs, handler=_tree),
        FunctionTool(name="read", description="Read a file's text (a document reads its sidecar), paginated.", params=_ReadArgs, handler=_read),
        FunctionTool(name="grep", description="Regex-search the text of documents' sidecars under a folder.", params=_GrepArgs, handler=_grep),
        FunctionTool(name="read_note", description="Read a folder's note describing what it holds.", params=_NoteArgs, handler=_read_note),
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
