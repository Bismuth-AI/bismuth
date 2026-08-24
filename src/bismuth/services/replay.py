"""Rebuild a folder tree from existing document sidecars."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from bismuth.domain.charter import CHARTER_FILENAME
from bismuth.domain.document import (
    Coverage,
    DocumentCard,
    Entity,
    EntityKind,
    Extraction,
    Section,
    SourceRef,
    sidecar_name,
)
from bismuth.domain.journal import Operation, OperationKind
from bismuth.ports.catalog import Catalog
from bismuth.ports.vault import INBOX, Vault
from bismuth.services.ingest import Prepared
from bismuth.services.sidecar import read_sidecar_meta

SUMMARY_HEADING = "## 요약"
QUESTIONS_HEADING = "## 이 문서로 답할 수 있는 질문"
BODY_HEADING = "## 본문"


def read_prepared(
    vault: Vault,
    catalog: Catalog,
    *,
    under: PurePosixPath | None = None,
) -> list[Prepared]:
    """Restore prepared documents from sidecars without calling a model."""
    base = vault.root.joinpath(*(under.parts if under else ()))
    restored: list[Prepared] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix == ".md":
            continue
        rel = PurePosixPath(path.relative_to(vault.root).as_posix())
        if under is None and any(part.startswith(("_", ".")) for part in rel.parts[:-1]):
            continue
        sidecar = path.with_name(sidecar_name(path.name))
        if not sidecar.is_file():
            continue
        if prepared := _from_sidecar(rel, path, sidecar, catalog):
            restored.append(prepared)
    return restored


def _from_sidecar(
    rel: PurePosixPath,
    path: Path,
    sidecar: Path,
    catalog: Catalog,
) -> Prepared | None:
    text = sidecar.read_text(encoding="utf-8", errors="replace")
    meta = read_sidecar_meta(text)
    if not meta:
        return None
    title = str(meta.get("title") or path.stem)
    summary = _section(text, SUMMARY_HEADING) or title
    digest = str(meta.get("sha256") or "")
    if len(digest) != 64:
        digest = SourceRef.hash_bytes(path.read_bytes())
    document_id = str(meta.get("document_id") or digest[:16])
    card = catalog.load_card(document_id) or _card_from_sidecar(meta, text, title, summary)
    source = catalog.load_source(document_id) or SourceRef(
        path=path,
        filename=path.name,
        size_bytes=path.stat().st_size,
        sha256=digest,
        modified_at=_moment(meta.get("modified_at"), path),
    )
    extraction = Extraction(
        sections=(Section(text=_body(text) or summary, order=0),),
        parser=str(meta.get("parser") or "sidecar"),
        truncated=bool(meta.get("truncated")),
    )
    return Prepared(
        rel=rel,
        source=source,
        card=card,
        extraction=extraction,
        preserve_sidecar=True,
        preserve_catalog=True,
    )


def _card_from_sidecar(
    meta: dict[str, object], text: str, title: str, summary: str
) -> DocumentCard:
    coverage = None
    if isinstance(meta.get("coverage"), dict):
        with suppress(ValueError):
            coverage = Coverage.model_validate(meta["coverage"])
    return DocumentCard(
        title=title,
        summary=summary,
        doc_type=str(meta.get("doc_type") or "document"),
        language=str(meta.get("language") or ""),
        topics=tuple(str(item) for item in _list(meta.get("topics"))),
        keywords=tuple(str(item) for item in _list(meta.get("keywords"))),
        entities=tuple(_entities(meta.get("entities"))),
        answers_questions=tuple(_bullets(_section(text, QUESTIONS_HEADING))),
        coverage=coverage,
    )


def _section(text: str, heading: str) -> str:
    if (start := text.find(heading)) < 0:
        return ""
    rest = text[start + len(heading) :]
    end = rest.find("\n## ")
    return rest[: end if end >= 0 else None].strip().lstrip("- ").strip()


def _body(text: str) -> str:
    body = _section(text, BODY_HEADING)
    return body.split("\n---\n\n_키워드:", 1)[0].rstrip()


def _bullets(block: str) -> list[str]:
    return [line.lstrip("- ").strip() for line in block.splitlines() if line.strip()]


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _entities(value: object) -> list[Entity]:
    entities: list[Entity] = []
    for item in _list(value):
        if not isinstance(item, dict):
            continue
        try:
            entities.append(
                Entity(name=str(item.get("name", "")), kind=EntityKind(str(item.get("kind"))))
            )
        except ValueError:
            continue
    return entities


def _moment(value: object, path: Path) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)


@dataclass(frozen=True, slots=True)
class Emptied:
    documents: int
    folders: int
    operations: tuple[Operation, ...]


def emptying(vault: Vault, *, into: PurePosixPath | None = None) -> Emptied:
    """Move documents and sidecars out of folders, then remove the empty tree."""
    destination = into or PurePosixPath()
    folders = [
        folder
        for folder in vault.iter_folders()
        if folder.parts and folder.parts[0] != INBOX.parts[0]
    ]
    operations: list[Operation] = []
    if destination.parts and not vault.exists(destination):
        operations.append(Operation(kind=OperationKind.MKDIR, target=destination))

    documents = 0
    claimed: set[str] = set()
    for folder in [*sorted(folders, key=lambda item: len(item.parts)), PurePosixPath()]:
        if folder == destination:
            continue
        for source in sorted(vault.iter_files(folder, recursive=False)):
            target = vault.unique_target(destination, source.name)
            while target.name.casefold() in claimed:
                target = vault.unique_target(destination, _next_name(target.name))
            claimed.add(target.name.casefold())
            operations.append(Operation(kind=OperationKind.MOVE, source=source, target=target))
            documents += 1
            sidecar = folder / sidecar_name(source.name)
            if vault.exists(sidecar):
                operations.append(
                    Operation(
                        kind=OperationKind.MOVE,
                        source=sidecar,
                        target=target.parent / sidecar_name(target.name),
                    )
                )
        charter = folder / CHARTER_FILENAME
        if vault.exists(charter):
            operations.append(Operation(kind=OperationKind.REMOVE, target=charter))

    operations.extend(
        Operation(kind=OperationKind.RMDIR, target=folder)
        for folder in sorted(folders, key=lambda item: len(item.parts), reverse=True)
    )
    return Emptied(documents, len(folders), tuple(operations))


def _next_name(filename: str) -> str:
    stem, dot, suffix = filename.rpartition(".")
    stem, suffix = (stem, f".{suffix}") if dot else (filename, "")
    if stem.endswith(")") and " (" in stem:
        head, _, count = stem.rpartition(" (")
        if count[:-1].isdigit():
            return f"{head} ({int(count[:-1]) + 1}){suffix}"
    return f"{stem} (2){suffix}"
