"""Pure batching, naming, and validation helpers for structured local growth."""

# ruff: noqa: E402, F401 -- isolated legacy helpers retain shared diagnostic types


from __future__ import annotations

import asyncio
import logging
import unicodedata
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TypeVar

from bismuth.domain.charter import CHARTER_FILENAME, Charter, boundary_purpose, routing_purpose
from bismuth.domain.document import DocumentCard, sidecar_name
from bismuth.domain.errors import BismuthError
from bismuth.domain.journal import Actor, JournalEntry, Operation, OperationKind
from bismuth.domain.maintenance import ProposedClass, normalise_label, validate_plan
from bismuth.domain.paths import sanitize_segment
from bismuth.domain.progress import Progress, ProgressSink, Stage, report
from bismuth.logging_setup import log_trace
from bismuth.ports.catalog import Catalog
from bismuth.ports.llm import LLM, Prompt
from bismuth.ports.vault import INBOX, STATE_DIR, Vault
from bismuth.prompts import subdivision as prompts
from bismuth.services.charters import CharterService
from bismuth.services.sidecar import read_sidecar_meta
from bismuth.services.transactor import Transactor

logger = logging.getLogger(__name__)

# Character budgeting is deliberately provider-neutral.  Tokenizers differ, but a
# 32k-character ceiling leaves a wide margin inside the smallest supported 65k-token
# context even with schema/tool framing.  Every maintenance call is built and measured
# before it reaches the adapter.
MAX_MAINTENANCE_PROMPT_CHARS = 32_000
PacketT = TypeVar("PacketT")
from bismuth.services.legacy.subdivision.models import _Contents


def _normalise(text: str) -> str:
    return "".join(text.split()).casefold()


def _same_name(name: str, ancestors: tuple[str, ...]) -> bool:
    return any(_normalise(name) == _normalise(part) for part in ancestors)


def _same_axis(proposed: str, spent: list[str]) -> bool:
    """Whether an axis has already been used somewhere above."""
    wanted = normalise_label(proposed)
    return any(wanted == normalise_label(used) for used in spent)


def _writing_system(text: str) -> str | None:
    """Return the dominant Unicode writing system in ``text``, when it is clear.

    This deliberately identifies scripts rather than languages.  The library can be
    handed any corpus, so a list of Korean legal terms (or English medical terms) would
    be an application-specific heuristic.  Unicode script names let us catch a model
    unexpectedly translating all of its signs while leaving mixed-language collections
    and ordinary borrowed words alone.
    """
    counts: Counter[str] = Counter()
    for character in text:
        if not character.isalpha():
            continue
        name = unicodedata.name(character, "")

        if any(
            marker in name
            for marker in ("HANGUL", "CJK UNIFIED", "IDEOGRAPH", "HIRAGANA", "KATAKANA")
        ):
            counts["east-asian"] += 1
            continue
        for marker, script in (
            ("LATIN", "latin"),
            ("CYRILLIC", "cyrillic"),
            ("ARABIC", "arabic"),
            ("HEBREW", "hebrew"),
            ("DEVANAGARI", "devanagari"),
            ("THAI", "thai"),
            ("GREEK", "greek"),
        ):
            if marker in name:
                counts[script] += 1
                break

    total = sum(counts.values())
    if not total:
        return None
    script, count = counts.most_common(1)[0]
    return script if count / total >= 0.60 else None


def _boundary_wording_problem(contents: _Contents, plan: prompts.Division) -> str | None:
    """Reject unusable sign wording before it can become filesystem structure.

    Semantic correctness still belongs to the model audits.  These are mechanical UX
    invariants: a folder note is a short routing hint, and a proposal must not silently
    translate a corpus whose own writing system is unambiguous.
    """
    if len(contents.scripts) < 2:
        return None
    source_counts = Counter(contents.scripts)
    source_script, source_count = source_counts.most_common(1)[0]
    if source_count / len(contents.scripts) < 0.75:
        return None

    # Only group names become visible filesystem signs.  ``basis`` and
    # ``basis_question`` are internal charter metadata and may legitimately be in the
    # prompt's language (often English) even when every visible sign is Korean.  Mixing
    # those fields here made a valid Korean sign look Latin and rejected the plan.
    wording = " ".join(group.name for group in plan.groups)
    proposed_script = _writing_system(wording)
    if proposed_script is not None and proposed_script != source_script:
        return "boundary wording uses a different writing system from its documents"
    return None


def _describe(card: DocumentCard) -> str:
    """The card evidence used for grouping; never the original document bytes."""
    topics = ", ".join(card.topics)
    parts = [card.title, card.doc_type]
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


def _value_packets(
    items: list[PacketT], build: Callable[[list[PacketT]], Prompt]
) -> list[list[PacketT]]:
    """Bound arbitrary compact metadata lists such as signs or boundary groups."""
    packets: list[list[PacketT]] = []
    current: list[PacketT] = []
    for item in items:
        candidate = [*current, item]
        if current and _prompt_chars(build(candidate)) > MAX_MAINTENANCE_PROMPT_CHARS:
            packets.append(current)
            current = [item]
        else:
            current = candidate
        if _prompt_chars(build(current)) > MAX_MAINTENANCE_PROMPT_CHARS:
            raise BismuthError("one maintenance sign exceeds context budget")
    if current:
        packets.append(current)
    return packets


def _relevant_children(
    documents: list[tuple[str, str]], children: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    names: set[str] = set()
    for _, description in documents:
        if not description.startswith("current="):
            continue
        current = description.removeprefix("current=").split(" | ", 1)[0]
        parts = PurePosixPath(current).parts
        if len(parts) > 1:
            names.add(parts[0])
    return [child for child in children if child[0] in names]


def _groups_for_ids(groups: list[prompts.Group], ids: set[str]) -> list[prompts.Group]:
    return [
        group.model_copy(
            update={
                "document_ids": [
                    document_id for document_id in group.document_ids if document_id in ids
                ]
            }
        )
        for group in groups
    ]


def _groups_relevant_to_ids(groups: list[prompts.Group], ids: set[str]) -> list[prompts.Group]:
    return [group for group in groups if ids.intersection(group.document_ids)]


def _sketch_packets(
    folder: PurePosixPath, sketches: list[prompts.ReplacementSketch]
) -> list[list[prompts.ReplacementSketch]]:
    packets: list[list[prompts.ReplacementSketch]] = []
    current: list[prompts.ReplacementSketch] = []
    for sketch in sketches:
        candidate = [*current, sketch]
        prompt = prompts.build_replacement_reduce(path=str(folder), sketches=candidate)
        if current and _prompt_chars(prompt) > MAX_MAINTENANCE_PROMPT_CHARS:
            packets.append(current)
            current = [sketch]
        else:
            current = candidate
    if current:
        packets.append(current)
    return packets


def _emerging_packets(
    *,
    folder: PurePosixPath,
    purpose: str,
    axis: str,
    children: list[tuple[str, str]],
    candidates: list[prompts.Emerging],
) -> list[list[prompts.Emerging]]:
    packets: list[list[prompts.Emerging]] = []
    current: list[prompts.Emerging] = []
    for candidate in candidates:
        proposed = [*current, candidate]
        prompt = prompts.build_emerging_reduce(
            path=str(folder),
            purpose=purpose,
            axis=axis,
            children=children,
            candidates=proposed,
        )
        if current and _prompt_chars(prompt) > MAX_MAINTENANCE_PROMPT_CHARS:
            packets.append(current)
            current = [candidate]
        else:
            current = proposed
    if current:
        packets.append(current)
    return packets


def _normalise_sketch(sketch: prompts.ReplacementSketch) -> prompts.ReplacementSketch:
    return sketch


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


def _free_filename(filename: str, taken: set[str]) -> str:
    """Choose a case-insensitively unique name inside one replacement class."""
    if filename.casefold() not in taken:
        return filename
    stem, dot, extension = filename.rpartition(".")
    stem, extension = (stem, f".{extension}") if dot else (filename, "")
    index = 2
    while True:
        candidate = f"{stem} ({index}){extension}"
        if candidate.casefold() not in taken:
            return candidate
        index += 1


def _in_inbox(path: PurePosixPath) -> bool:
    return bool(path.parts) and path.parts[0] == INBOX.parts[0]


def _failed_boundary_checks(audit: prompts.BoundaryAudit) -> list[str]:
    failed = [
        name
        for name in (
            "one_property",
            "names_answer_question",
            "mutually_exclusive",
            "useful_for_navigation",
            "members_match_signs",
            "no_remainder_sign",
        )
        if not getattr(audit, name)
    ]
    failed.extend(f"violation:{item}" for item in audit.violations)
    return failed


def _failed_routing_checks(audit: prompts.RoutingAudit) -> list[str]:
    return [
        name
        for name in ("assignments_match_signs", "no_forced_fit")
        if not getattr(audit, name)
    ]
