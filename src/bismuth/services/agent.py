"""Agents over the vault, built on the standalone ``agentkit`` library.

Two shapes, same tools underneath:
- ``ask``: read-only navigation (ls/tree/read/grep/note) to answer a question.
- ``organize``: the same plus an approval-gated ``move`` to reshape the folder tree.

bismuth supplies the model, the tools (thin wrappers over the vault/services), and
the prompt; agentkit runs the loop.
"""

from __future__ import annotations

import bisect
import difflib
import itertools
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from agentkit import Agent, ChatModel, FunctionTool, RunResult, Tool, subagent_tool
from agentkit.loop import OnEvent
from pydantic import BaseModel, Field

from bismuth.domain.charter import CHARTER_FILENAME
from bismuth.domain.document import sidecar_name
from bismuth.ports.vault import INBOX, Vault
from bismuth.services.charters import CharterService
from bismuth.services.sidecar import read_sidecar_meta

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
   Treat a folder's `_folder.md` as evidence of its intended stable boundary, not \
as proof that the current contents still satisfy it. Judge that sign together with \
the documents' ACTUAL types (shown in `ls` as `[type]`) and the folder's name. When \
the actual documents no longer satisfy the recorded boundary, \
the folder may need splitting or renaming.
3. When you act, choose the lighter fix:
   - If the grouping is fine but the folder's NAME no longer fits its contents, \
`rename` the folder. Do not split what does not need splitting.
   - If genuinely different things are piled together, PROPOSE `move`s: group \
documents into subfolders by one distinction supported by the documents and useful \
for ruling alternatives out, in the documents' own language. Reuse the right existing \
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

    A sidecar is text pulled out of a PDF, so it is hard-wrapped at whatever width the
    page had, and a phrase lands on two lines often enough to matter: 방문판매법 시행령
    holds `연 100분` / `의 15를 말한다`, which a line-at-a-time search cannot see. Over
    this corpus a whitespace-blind pass finds documents the line-based one misses for
    one search phrase in three.

    Returns None when removing the whitespace would change what the pattern means --
    ``^`` and ``$`` are per-line, and there are no lines left to anchor to.
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
    # Most documents hold no match at all, so ask the cheap question first: strip the
    # whole text in one pass and look. Only a document that answers yes pays for the
    # line map that turns a position back into a line number.
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
        """The shape of the vault, one folder per line, each line a usable path.

        Indentation alone used to carry the nesting, so a caller wanting to look inside
        a folder had to rebuild its path by counting spaces up the whole listing. That
        is a step at which to be wrong, and it was: an agent asked for
        ``산업 진흥/1인 창조기업`` when the folder was under ``창업 지원``, got a bare
        "no such folder", and spent its entire budget without ever opening a document.
        """
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
            # One document: the caller wants every place inside it, not a sample --
            # comparing what a file contains is impossible from five lines and a count.
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

        # Grouped by file, because a hit is a pointer to a place -- repeating the path
        # for every line spends the result's whole budget saying the same thing, and
        # what the reader needs first is which documents to look in.
        rows: list[str] = []
        for file, lines in found[:_GREP_FILE_LIMIT]:
            rows.append(str(file))
            rows.extend(lines[:per_file])
            if len(lines) > per_file:
                rows.append(f"  … 이 문서에서 {len(lines)} 곳")
        if len(found) > _GREP_FILE_LIMIT:
            rows.append("… 다른 문서에도 더 있다. 폴더나 표현을 좁혀서 다시 찾아라.")
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
