"""Re-file a collection that has already been read, without reading it again.

Cataloguing costs 99% of a run's model time and depends on nothing but the document, so
tuning the part that decides *folders* by re-reading three hundred documents each time
pays twenty minutes for sixty-nine seconds of the thing under test.

Every card is already on disk: the sidecar beside each document carries the title, the
kind, the topics and the language the filing prompt is shown. This lifts them back out and
runs the same :class:`SimpleFiler` against a fresh vault, so what is measured is the
placement code and the prompts, and nothing else moved.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from bismuth.domain.charter import CHARTER_FILENAME
from bismuth.domain.document import (
    DocumentCard,
    Entity,
    EntityKind,
    Extraction,
    Section,
    SourceRef,
    sidecar_name,
)
from bismuth.domain.journal import Operation, OperationKind
from bismuth.ports.vault import INBOX, Vault
from bismuth.services.sidecar import read_sidecar_meta

logger = logging.getLogger(__name__)

SUMMARY_HEADING = "## 요약"
QUESTIONS_HEADING = "## 이 문서로 답할 수 있는 질문"
BODY_HEADING = "## 본문"


@dataclass(frozen=True, slots=True)
class Pinned:
    """One document as it was already understood: the file, and what was concluded about it.

    Stands in for ``Prepared`` where the filer only needs the source and the extraction to
    write a sidecar; the card no longer has to be earned.
    """

    path: Path
    card: DocumentCard
    source: SourceRef
    extraction: Extraction

    @property
    def name(self) -> str:
        return self.path.name


def read_pinned(vault: Path, *, under: str = "") -> list[Pinned]:
    """Every document that still has a sidecar to be read back from.

    ``under`` names one folder to look in instead of the tree, and is how the inbox gets
    read: it is skipped by name everywhere else, which is the point of it.
    """
    out: list[Pinned] = []
    for path in sorted((vault / under if under else vault).rglob("*")):
        if not path.is_file() or path.suffix == ".md":
            continue
        inside = path.relative_to(vault).parts[:-1]
        if not under and any(part.startswith(("_", ".")) for part in inside):
            continue
        beside = path.parent / sidecar_name(path.name)
        if not beside.is_file():
            continue
        if (pinned := _from_sidecar(path, beside)) is not None:
            out.append(pinned)
    return out


def _from_sidecar(path: Path, sidecar: Path) -> Pinned | None:
    text = sidecar.read_text(encoding="utf-8", errors="replace")
    meta = read_sidecar_meta(text)
    if not meta:
        return None
    title = str(meta.get("title") or path.stem)
    summary = _section(text, SUMMARY_HEADING) or title
    card = DocumentCard(
        title=title,
        summary=summary,
        doc_type=str(meta.get("doc_type") or "문서"),
        language=str(meta.get("language") or ""),
        topics=tuple(str(t) for t in _listed(meta.get("topics"))),
        keywords=tuple(str(k) for k in _listed(meta.get("keywords"))),
        entities=tuple(_entities(meta.get("entities"))),
        answers_questions=tuple(_bullets(_section(text, QUESTIONS_HEADING))),
    )
    source = SourceRef(
        path=path,
        filename=str(meta.get("source") or path.name),
        size_bytes=_count(meta.get("size_bytes"), path),
        sha256=str(meta.get("sha256") or ""),
        modified_at=_moment(meta.get("modified_at")),
    )
    body = _section(text, BODY_HEADING)
    extraction = Extraction(
        sections=(Section(text=body or summary, order=0),),
        parser=str(meta.get("parser") or "replay"),
        truncated=bool(meta.get("truncated")),
    )
    return Pinned(path=path, card=card, source=source, extraction=extraction)


def _section(text: str, heading: str) -> str:
    """The prose under one heading, up to the next one."""
    if (at := text.find(heading)) < 0:
        return ""
    rest = text[at + len(heading) :]
    end = rest.find("\n## ")
    return rest[: end if end >= 0 else None].strip().lstrip("-").strip()


def _bullets(block: str) -> list[str]:
    return [line.lstrip("- ").strip() for line in block.splitlines() if line.strip()]


def _listed(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _entities(value: object) -> list[Entity]:
    out = []
    for item in _listed(value):
        if not isinstance(item, dict):
            continue
        try:
            out.append(
                Entity(name=str(item.get("name", "")), kind=EntityKind(str(item.get("kind"))))
            )
        except ValueError:
            continue  # a kind this build no longer has
    return out


def _count(value: object, path: Path) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return path.stat().st_size


def _moment(value: object) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.now(UTC)


def staged(pinned: Pinned, rel: PurePosixPath) -> tuple[PurePosixPath, DocumentCard, Pinned]:
    """The triple the filer takes: where it sits now, what it is, and what to write beside it."""
    return (rel, pinned.card, pinned)


def here(pinned: Pinned, vault: Vault) -> tuple[PurePosixPath, DocumentCard, Pinned]:
    """The same triple for a document that is already in the vault and is not moving out."""
    return (PurePosixPath(pinned.path.relative_to(vault.root).as_posix()), pinned.card, pinned)


def _next_name(filename: str) -> str:
    """``a (2).pdf`` from ``a.pdf``, and ``a (3).pdf`` from that."""
    stem, dot, extension = filename.rpartition(".")
    stem, extension = (stem, f".{extension}") if dot else (filename, "")
    if stem.endswith(")") and " (" in stem:
        head, _, count = stem.rpartition(" (")
        if count[:-1].isdigit():
            return f"{head} ({int(count[:-1]) + 1}){extension}"
    return f"{stem} (2){extension}"


@dataclass(frozen=True, slots=True)
class Emptied:
    """What taking the tree apart came to."""

    documents: int
    folders: int
    operations: tuple[Operation, ...]


def emptying(vault: Vault, *, into: PurePosixPath | None = None) -> Emptied:
    """Every document out of the tree, every folder gone. Documents and cards survive.

    The tree is the only thing under test; the reading of three hundred documents behind it
    is not. This is what lets the same collection be filed again from a clean root without
    paying for it twice -- and being one journal entry, it is one undo away from before.

    ``into`` is where the documents go. The root, by default, which is where a person
    expects to find them. The inbox when the collection is about to be filed again from
    the start: a document at the root is already *in* the collection, so leaving three
    hundred there means the first review sees the whole corpus at once and reorganises it
    in one move -- which is not what filing ten at a time does, and not what is being
    measured.
    """
    landing_folder = PurePosixPath() if into is None else into
    operations: list[Operation] = []
    documents = 0
    folders = [
        folder
        for folder in vault.iter_folders()
        if folder.parts and folder.parts[0] != INBOX.parts[0]
    ]
    if landing_folder.parts and not vault.exists(landing_folder):
        operations.append(Operation(kind=OperationKind.MKDIR, target=landing_folder))
    # The root is swept too, not just the folders: documents sitting loose are as much
    # part of the collection as filed ones, and a re-filing that skipped them would leave
    # the pile that most needs deciding exactly where it was.
    swept = [*sorted(folders, key=lambda f: len(f.parts)), PurePosixPath()]
    claimed: set[str] = set()
    """Names this plan has already given away. ``unique_target`` reads the disk, where
    none of these exist yet, so two same-named documents from two folders would both be
    told to land on the same name."""
    for folder in swept:
        if folder == landing_folder:
            continue
        for path in sorted(vault.iter_files(folder, recursive=False)):
            if path.name.endswith(".md"):
                continue
            landing = vault.unique_target(landing_folder, path.name)
            while landing.name.casefold() in claimed:
                landing = vault.unique_target(landing_folder, _next_name(landing.name))
            claimed.add(landing.name.casefold())
            operations.append(
                Operation(kind=OperationKind.MOVE, source=path, target=landing, note="tree emptied")
            )
            documents += 1
            beside = folder / sidecar_name(path.name)
            if vault.exists(beside):
                operations.append(
                    Operation(
                        kind=OperationKind.MOVE,
                        source=beside,
                        target=landing.parent / sidecar_name(landing.name),
                        note="sidecar",
                    )
                )
        note = folder / CHARTER_FILENAME
        if vault.exists(note):
            # The note describes a folder that is about to stop existing. Left behind it
            # would also keep the folder un-empty, and an un-empty folder is never removed.
            operations.append(Operation(kind=OperationKind.REMOVE, target=note, note="folder gone"))
    operations.extend(
        Operation(kind=OperationKind.RMDIR, target=folder)
        for folder in sorted(folders, key=lambda f: len(f.parts), reverse=True)
    )
    return Emptied(documents=documents, folders=len(folders), operations=tuple(operations))
