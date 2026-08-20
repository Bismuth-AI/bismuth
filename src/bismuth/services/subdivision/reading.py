"""What a folder looks like to a prompt: cards, not documents."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from bismuth.domain.document import DocumentCard
from bismuth.domain.errors import BismuthError
from bismuth.domain.maintenance import (
    normalise_label,
)
from bismuth.ports.llm import Prompt

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Divided:
    """What dividing one folder did."""

    folder: PurePosixPath
    created: tuple[PurePosixPath, ...] = ()
    moved: int = 0
    basis: str = ""
    routed: bool = False
    """True when documents were moved into folders that already existed, rather than
    into a class created here. Those folders just gained evidence they did not have."""

    @property
    def happened(self) -> bool:
        return bool(self.created) or self.moved > 0


# Character budgeting is deliberately provider-neutral.  Tokenizers differ, but a
# 32k-character ceiling leaves a wide margin inside the smallest supported 65k-token
# context even with schema/tool framing.  Every maintenance call is built and measured
# before it reaches the adapter.
MAX_MAINTENANCE_PROMPT_CHARS = 32_000


@dataclass(slots=True)
class _Contents:
    """One folder as the model is shown it: cards, not documents."""

    documents: list[tuple[str, str, PurePosixPath]] = field(default_factory=list)
    """(document_id, one-line description, file path)."""
    children: list[tuple[str, str]] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    """Dominant writing system of each document title, when one is detectable."""
    languages: list[str] = field(default_factory=list)
    """The language each card reported, so a prompt can name it back."""

    topics: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    """What each card says its document is about, for the one question that has to cover
    the whole folder rather than the handful that prompted it."""

    subjects: list[tuple[str, str]] = field(default_factory=list)
    """The same documents without their doc_type, for the one question that must not be
    answered with it. Grouping by the kind of instrument a document is fills a tree
    neatly and separates nothing a reader needs, and the old prompt argued against it in
    prose while the column sat in the evidence: gpt-5-nano grouped the three 시행규칙 out
    of seven documents whose subjects were unrelated."""

    @property
    def lines(self) -> list[tuple[str, str]]:
        return [(document_id, line) for document_id, line, _ in self.documents]

    @property
    def subject_lines(self) -> list[tuple[str, str]]:
        return list(self.subjects)

    @property
    def language(self) -> str:
        """The language to answer in, when the collection agrees on one.

        Read off the cards rather than assumed. An English instruction produces English
        folder names over a Korean archive unless the prompt says otherwise -- observed
        as twelve rejected proposals in one round, all of them named in English. Naming
        the collection's own language back to the model is evidence, not a builtin.
        """
        if not self.languages:
            return ""
        code, count = Counter(self.languages).most_common(1)[0]
        return code if count / len(self.languages) >= 0.75 else ""

    def path_of(self, document_id: str) -> PurePosixPath | None:
        return next((p for i, _, p in self.documents if i == document_id), None)


def _vocabulary(contents: _Contents, *, taken: set[str], most: int = 40) -> list[str]:
    """What the folder is about, minus the documents already spoken for.

    Topics rather than titles, deliberately: shown titles, the axis step read the kind of
    instrument off them. Ordered by how many documents carry each one, so a question that
    covers the front of this list covers most of the folder.
    """
    counted: Counter[str] = Counter()
    for document_id, topics in contents.topics:
        if document_id in taken:
            continue
        counted.update(topic.strip() for topic in topics if topic.strip())
    return [topic for topic, _ in counted.most_common(most)]


def _shown_fingerprint(pairs: list[tuple[str, str]]) -> str:
    """What a question offered, as one comparable string.

    An answer depends on what was in front of it and on nothing else, so a memory of that
    answer lasts exactly as long as the list does. Used for the signs a routing question
    offered, and for the pile a grouping question was asked about.
    """
    return "\u0000".join(f"{name}\u0001{note}" for name, note in sorted(pairs))


def _describe(card: DocumentCard, *, with_type: bool = True) -> str:
    """The card evidence used for grouping; never the original document bytes."""
    topics = ", ".join(card.topics)
    parts = [card.title, card.doc_type] if with_type else [card.title]
    if topics:
        parts.append(topics)
    if card.summary:
        parts.append(card.summary)
    return " | ".join(parts)


def _prompt_chars(prompt: Prompt) -> int:
    return len(prompt.system) + len(prompt.user)


def _document_packets(
    documents: list[tuple[str, str]],
    build: Callable[[list[tuple[str, str]]], Prompt],
    *,
    max_documents: int | None = None,
) -> list[list[tuple[str, str]]]:
    """Partition evidence by input context and, when needed, output cardinality."""
    if max_documents is not None and max_documents < 1:
        raise ValueError("max_documents must be positive")
    if not documents:
        if _prompt_chars(build([])) > MAX_MAINTENANCE_PROMPT_CHARS:
            raise BismuthError("maintenance metadata exceeds context budget")
        return [[]]
    packets: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    for document in documents:
        candidate = [*current, document]
        output_would_overflow = bool(
            current and max_documents is not None and len(candidate) > max_documents
        )
        if current and (
            output_would_overflow or _prompt_chars(build(candidate)) > MAX_MAINTENANCE_PROMPT_CHARS
        ):
            packets.append(current)
            current = [document]
        else:
            current = candidate
        if _prompt_chars(build(current)) > MAX_MAINTENANCE_PROMPT_CHARS:
            # A card is already a summary rather than original bytes. Pathological legacy
            # cards can still be larger than a whole request; retain the handle and prefix
            # instead of sending an over-context request that the provider must reject.
            document_id, description = current[0]
            empty_size = _prompt_chars(build([(document_id, "")]))
            allowance = MAX_MAINTENANCE_PROMPT_CHARS - empty_size - 64
            if allowance <= 0:
                raise BismuthError("boundary metadata alone exceeds maintenance context budget")
            current = [(document_id, description[:allowance])]
    if current:
        packets.append(current)
    return packets


def _quotes_evidence(wording: str, documents: list[tuple[str, str]]) -> bool:
    """Whether a proposed axis or name is copied out of the documents in front of it.

    An axis is the name of a property; a document title is a value of nothing. Observed
    live: a replacement returned the axis "소상공인 보호 및 지원에 관한 법률, 공인회계사법,
    보험업법 시행령, 서민의 금융생활 지원에 관한 법률" -- four titles from the packet,
    joined by commas, recorded on the folder and shown to every later question about it.

    No vocabulary: the comparison is against the evidence in the same request, so it
    means the same thing in any language and for any collection. Short titles are
    skipped because a two-character title carries no evidence of copying.
    """
    text = normalise_label(wording)
    if not text:
        return False
    for _, description in documents:
        title = description.split(" | ")[0].removeprefix("current=").strip()
        key = normalise_label(title)
        if len(key) >= 8 and key in text:
            return True
    return False


async def _bounded_gather(
    documents: list[tuple[str, str]],
    worker: Callable[[tuple[str, str]], Awaitable[tuple[str, str]]],
) -> list[tuple[str, str]]:
    """Run small independent choices with bounded pressure on a local model server."""
    semaphore = asyncio.Semaphore(4)

    async def run(document: tuple[str, str]) -> tuple[str, str]:
        async with semaphore:
            return await worker(document)

    return list(await asyncio.gather(*(run(document) for document in documents)))
