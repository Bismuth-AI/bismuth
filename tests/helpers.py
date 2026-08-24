"""Shared builders for service tests."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from bismuth.container import Bismuth
from bismuth.domain.charter import CHARTER_FILENAME, Charter
from bismuth.services.sidecar import render_sidecar


async def add(
    engine: Bismuth,
    name: str,
    body: str = "Generic project report, 2023.",
) -> SimpleNamespace:
    return await add_into(engine, name, "아폴로/2023", body)


async def add_into(
    engine: Bismuth,
    name: str,
    folder: str,
    body: str = "",
) -> SimpleNamespace:
    """Place a prepared fixture at a known path."""
    rel = engine.ingest.stage((body or f"unique content for {name}").encode(), name)
    prepared = await engine.ingest.prepare(rel)
    assert prepared.card is not None and prepared.extraction is not None

    destination = PurePosixPath(folder)
    absolute = Path(engine.vault.root) / Path(*destination.parts)
    absolute.mkdir(parents=True, exist_ok=True)
    for level in range(1, len(destination.parts) + 1):
        path = PurePosixPath(*destination.parts[:level])
        target = Path(engine.vault.root) / Path(*path.parts) / CHARTER_FILENAME
        if not target.exists():
            target.write_text(
                Charter(
                    path=path, title=path.name, purpose=f"Documents filed under {path}."
                ).to_markdown(),
                encoding="utf-8",
            )

    source = Path(engine.vault.root) / Path(*rel.parts)
    final = absolute / name
    source.replace(final)
    sidecar = final.with_name(f"{final.name}.md")
    sidecar.write_text(
        render_sidecar(
            source=prepared.source,
            card=prepared.card,
            extraction=prepared.extraction,
            document_id=prepared.source.document_id,
        ),
        encoding="utf-8",
    )
    engine.catalog.save_card(
        prepared.source.document_id,
        prepared.card,
        source=prepared.source,
    )
    return SimpleNamespace(document_id=prepared.source.document_id, destination=destination)
