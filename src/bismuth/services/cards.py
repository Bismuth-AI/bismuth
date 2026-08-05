"""Turns extracted text into a card describing what the document is and is about.

The whole document is read, not its opening. Text is cut by length into windows,
the card is revised window by window in reading order, facts accumulate as a union,
and a final pass pulls the facts that matter into the summary. No step asks the
document to have headings, pages or a table of contents, so a scanned memo and a
300-page contract go down the same path.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from bismuth.domain.document import Coverage, DocumentCard, Extraction, Window
from bismuth.domain.errors import StructuredOutputError
from bismuth.domain.progress import Progress, ProgressSink, Stage, report
from bismuth.logging_setup import log_trace
from bismuth.ports.llm import LLM, ModelProfile
from bismuth.prompts import cards as card_prompts

logger = logging.getLogger(__name__)

T = TypeVar("T")

_EMPTY_SUMMARY = "(요약 없음)"
_UNKNOWN_TYPE = "문서"


@dataclass(frozen=True, slots=True)
class _Folded:
    """One window's effect on the card. ``found`` is for the person waiting; ``contributed`` for coverage."""

    card: DocumentCard
    found: tuple[str, ...]
    contributed: bool
    failed: bool


class CardService:
    """Describes documents."""

    def __init__(
        self,
        llm: LLM,
        *,
        context_chars: int = 12_000,
        max_windows: int = 16,
    ) -> None:
        self._llm = llm
        self._context_chars = context_chars
        self._max_windows = max_windows

    async def describe(
        self,
        extraction: Extraction,
        *,
        filename: str,
        document_id: str = "-",
        on_progress: ProgressSink | None = None,
    ) -> DocumentCard:
        """Read a document and say what it is and what it is about."""
        started = time.perf_counter()
        windows = extraction.windows(self._context_chars) or (
            Window(index=0, total=1, start=0, end=0, text=""),
        )
        selected = _evenly_spaced(windows, self._max_windows)
        chars_total = windows[-1].end

        log_trace(
            "card.begin",
            document_id=document_id,
            filename=filename,
            parser=extraction.parser,
            chars_total=chars_total,
            window_chars=self._context_chars,
            windows_total=len(windows),
            windows_selected=[w.index for w in selected],
            extraction_truncated=extraction.truncated,
        )
        if len(selected) < len(windows):
            # A budget cap that silently ate the middle of a document would read as
            # full coverage. Say which parts were dropped, in the log and on the card.
            read_indices = {w.index for w in selected}
            log_trace(
                "card.windows_skipped",
                document_id=document_id,
                filename=filename,
                reason=f"max_windows={self._max_windows}",
                skipped=[[w.start, w.end] for w in windows if w.index not in read_indices],
            )
            logger.warning(
                "%s: %d windows over the %d-window budget; reading %d spread across the document",
                filename,
                len(windows),
                self._max_windows,
                len(selected),
            )

        def step(position: int, found: tuple[str, ...] = ()) -> None:
            report(
                on_progress,
                Progress(
                    stage=Stage.READING,
                    filename=filename,
                    document_id=document_id,
                    step=position,
                    steps=len(selected),
                    found=found,
                ),
            )

        step(1)
        card = await self._first(
            selected[0],
            filename=filename,
            document_id=document_id,
            truncated=extraction.truncated,
        )
        contributed = 1  # the window the card was built from, by definition
        failed = 0

        for position, window in enumerate(selected[1:], start=2):
            step(position)
            folded = await self._fold(
                window, card=card, filename=filename, document_id=document_id, read=position
            )
            card = folded.card
            contributed += int(folded.contributed)
            failed += int(folded.failed)
            # Reported again after the call: the first report moves the bar, this one says
            # what the window actually turned up. A bar with nothing behind it is the thing
            # we are trying to get rid of.
            step(position, found=folded.found)

        coverage = Coverage(
            chars_total=chars_total,
            chars_read=sum(len(w.text) for w in selected),
            windows_total=len(windows),
            windows_read=len(selected),
            windows_contributed=contributed,
            windows_failed=failed,
            extraction_truncated=extraction.truncated,
        )

        if len(selected) > 1:
            report(
                on_progress,
                Progress(
                    stage=Stage.DENSIFYING,
                    filename=filename,
                    document_id=document_id,
                    step=len(selected),
                    steps=len(selected),
                ),
            )
            card = await self._densify(card, filename=filename, document_id=document_id)

        card = card.model_copy(update={"coverage": coverage})
        log_trace(
            "card.done",
            document_id=document_id,
            filename=filename,
            elapsed_ms=_ms(started),
            passes=len(selected) + (1 if len(selected) > 1 else 0),
            coverage=coverage.model_dump(),
            card=card.model_dump(mode="json", exclude={"coverage"}),
        )
        logger.info(
            "%s: card from %d/%d windows (%d contributed, %d failed) in %dms",
            filename,
            coverage.windows_read,
            coverage.windows_total,
            contributed,
            failed,
            _ms(started),
        )
        return card

    async def _first(
        self, window: Window, *, filename: str, document_id: str, truncated: bool
    ) -> DocumentCard:
        """The opening window. A failure here is fatal: there is no card to fall back on."""
        started = time.perf_counter()
        draft = await self._llm.structured(
            card_prompts.build(filename=filename, window=window, truncated=truncated),
            schema=card_prompts.CardDraft,
            profile=ModelProfile.FAST,
        )
        card = DocumentCard(
            title=draft.title.strip() or filename,
            summary=draft.summary.strip() or _EMPTY_SUMMARY,
            doc_type=draft.doc_type.strip() or _UNKNOWN_TYPE,
            topics=tuple(_clean(draft.topics)),
            entities=tuple(_unique(draft.entities, key=lambda e: e.key())),
            keywords=tuple(_clean(draft.keywords)),
            language=draft.language.strip() or "unknown",
            answers_questions=tuple(_clean(draft.answers_questions)),
        )
        log_trace(
            "card.window",
            document_id=document_id,
            filename=filename,
            **_window_fields(window),
            pass_kind="first",
            elapsed_ms=_ms(started),
            contributed=True,
            title=card.title,
            doc_type=card.doc_type,
            language=card.language,
            summary=card.summary,
            added={
                "topics": list(card.topics),
                "entities": [e.key() for e in card.entities],
                "keywords": list(card.keywords),
                "questions": list(card.answers_questions),
            },
        )
        return card

    async def _fold(
        self, window: Window, *, card: DocumentCard, filename: str, document_id: str, read: int
    ) -> _Folded:
        """Revise the card with one further window."""
        started = time.perf_counter()
        try:
            update = await self._llm.structured(
                card_prompts.build_update(filename=filename, window=window, card=card, read=read),
                schema=card_prompts.CardUpdate,
                profile=ModelProfile.FAST,
            )
        except StructuredOutputError as exc:
            # One unreadable window must not cost us the windows already read.
            log_trace(
                "card.window_failed",
                document_id=document_id,
                filename=filename,
                **_window_fields(window),
                elapsed_ms=_ms(started),
                error=str(exc),
            )
            logger.warning(
                "%s: window %s failed, keeping the card so far: %s", filename, window.label, exc
            )
            return _Folded(card=card, found=(), contributed=False, failed=True)

        topics = _added(card.topics, _clean(update.new_topics))
        entities = _added(card.entities, update.new_entities, key=lambda e: e.key())
        keywords = _added(card.keywords, _clean(update.new_keywords))
        questions = _added(card.answers_questions, _clean(update.new_questions))
        summary = update.summary.strip() or card.summary
        # New facts, not the model's self-report: asked whether it learned something, a
        # model says yes about a page of boilerplate. Both go to the trace so the
        # disagreement stays visible.
        # Topics and entity names, not keywords or questions: this is what gets shown
        # while the user waits, and it should read as things, not as prose.
        found = tuple(topics) + tuple(e.name for e in entities)
        added = bool(topics or entities or keywords or questions)

        revised = card.model_copy(
            update={
                "title": (update.title or "").strip() or card.title,
                "doc_type": (update.doc_type or "").strip() or card.doc_type,
                "summary": summary,
                "topics": card.topics + tuple(topics),
                "entities": card.entities + tuple(entities),
                "keywords": card.keywords + tuple(keywords),
                "answers_questions": card.answers_questions + tuple(questions),
            }
        )
        log_trace(
            "card.window",
            document_id=document_id,
            filename=filename,
            **_window_fields(window),
            pass_kind="update",
            elapsed_ms=_ms(started),
            contributed=added,
            model_said_contributed=update.contributed,
            note=update.note,
            retitled=revised.title if update.title else None,
            summary=revised.summary,
            added={
                "topics": topics,
                "entities": [e.key() for e in entities],
                "keywords": keywords,
                "questions": questions,
            },
        )
        return _Folded(card=revised, found=found, contributed=added, failed=False)

    async def _densify(
        self, card: DocumentCard, *, filename: str, document_id: str
    ) -> DocumentCard:
        """Close the gap between the facts gathered from the whole document and the summary.

        The last window rewrote the summary knowing every fact but weighting the text
        it had just read; this pass weighs the facts alone.
        """
        started = time.perf_counter()
        try:
            dense = await self._llm.structured(
                card_prompts.build_densify(card=card),
                schema=card_prompts.DensifiedSummary,
                profile=ModelProfile.FAST,
            )
        except StructuredOutputError as exc:
            log_trace(
                "card.densify_failed",
                document_id=document_id,
                filename=filename,
                elapsed_ms=_ms(started),
                error=str(exc),
            )
            logger.warning(
                "%s: densify pass failed, keeping the running summary: %s", filename, exc
            )
            return card

        summary = dense.summary.strip()
        if not summary:
            return card
        log_trace(
            "card.densified",
            document_id=document_id,
            filename=filename,
            elapsed_ms=_ms(started),
            absorbed=dense.absorbed,
            before=card.summary,
            after=summary,
            length_delta=len(summary) - len(card.summary),
        )
        return card.model_copy(update={"summary": summary})


def _window_fields(window: Window) -> dict[str, object]:
    """Enough of a window to find it in the source without storing it twice."""
    return {
        "window": window.index,
        "windows_total": window.total,
        "chars": [window.start, window.end],
        "text_head": window.text[:200],
        "text_tail": window.text[-100:],
    }


def _evenly_spaced(windows: Sequence[Window], limit: int) -> list[Window]:
    """At most ``limit`` windows, spread from the first to the last.

    Over budget, reading the first N windows would be the head-only bias this loop
    exists to remove, so the sample strides the whole document instead.
    """
    if limit <= 0 or len(windows) <= limit:
        return list(windows)
    if limit == 1:
        return [windows[0]]
    step = (len(windows) - 1) / (limit - 1)
    picked = sorted({round(i * step) for i in range(limit)})
    return [windows[i] for i in picked]


def _clean(values: Iterable[str]) -> list[str]:
    return [stripped for value in values if (stripped := value.strip())]


def _unique(values: Iterable[T], *, key: Callable[[T], str]) -> list[T]:
    seen: set[str] = set()
    kept: list[T] = []
    for value in values:
        identity = key(value)
        if identity not in seen:
            seen.add(identity)
            kept.append(value)
    return kept


def _added(
    existing: Sequence[T], incoming: Iterable[T], *, key: Callable[[T], str] | None = None
) -> list[T]:
    """The incoming items not already present. Facts are a union; nothing is ever dropped."""
    identity = key or (lambda value: " ".join(str(value).casefold().split()))
    seen = {identity(value) for value in existing}
    kept: list[T] = []
    for value in incoming:
        marker = identity(value)
        if marker not in seen:
            seen.add(marker)
            kept.append(value)
    return kept


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
