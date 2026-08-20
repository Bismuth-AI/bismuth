"""What every subdivision test needs to set a folder up and read what happened.

One definition of "a folder with four documents in it", so a test that moves between
operators keeps meaning the same thing.
"""

from __future__ import annotations

import contextlib
import importlib
from unittest import mock

from bismuth.container import Bismuth
from bismuth.prompts import placement as placement_prompts
from bismuth.prompts import subdivision as subdivision_prompts
from tests.conftest import ScriptedModel
from tests.test_ingest import add, place_at


@contextlib.contextmanager
def _traced():
    """Collect every trace event the maintenance service emits, wherever it emits it from.

    The service is a package now and each operator logs from its own module, so patching
    one of them would silently miss the others.
    """
    events: list[tuple[str, dict]] = []
    modules = [
        importlib.import_module(f"bismuth.services.subdivision.{name}")
        for name in ("emerging", "grouping", "splitting", "naming", "service")
    ]
    with contextlib.ExitStack() as stack:
        for module in modules:
            stack.enter_context(
                mock.patch.object(module, "log_trace", lambda e, **f: events.append((e, f)))
            )
        yield events


def _emerges(
    script: ScriptedModel, name: str, note: str, ids: list[str], *, axis: str = "주제"
) -> None:
    """Script a class coming out of the pile: its name, then who belongs to it."""
    script.set(
        subdivision_prompts.Emerging,
        subdivision_prompts.Emerging(
            emerged=True,
            axis=axis,
            axis_question=f"어느 {axis}에 속하는가?",
            name=name,
            sign=note,
        ),
    )
    script.set(
        subdivision_prompts.Members,
        subdivision_prompts.Members(document_ids=ids),
    )
    # Membership is one closed SHELF/STAY choice per document since ADR-0014; the
    # Members schema above no longer reaches this path.
    script.set_members(ids)


def _by_name(engine: Bismuth) -> dict[str, str]:
    return {
        source.filename: document_id
        for document_id, _ in engine.catalog.iter_cards()
        if (source := engine.catalog.load_source(document_id)) is not None
    }


async def _fill(engine: Bismuth, script: ScriptedModel, count: int) -> list[str]:
    """Put documents in root and return the short handles shown in one maintenance view."""
    script.set(placement_prompts.PlacementDecision, place_at(""))
    for index in range(count):
        await add(engine, f"doc{index}.txt", f"문서 {index} 내용")
    return [f"D{index:04d}" for index in range(1, count + 1)]


def _routing(prompt) -> bool:  # type: ignore[no-untyped-def]
    """A loose document being offered the signs that already stand in its folder.

    Shares the F### handle shape with a placement descent, so the descent marker is what
    tells them apart -- the same rule the scripted model routes on.
    """
    return "\n  [F" in prompt.user and "CURRENT FOLDER:" not in prompt.user
