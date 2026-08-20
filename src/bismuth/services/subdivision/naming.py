"""The mechanical checks a proposal is held to before any model sees it."""

from __future__ import annotations

import logging
import unicodedata
from collections import Counter
from pathlib import PurePosixPath

from bismuth.domain.charter import (
    routing_sign,
    sign_refusal,
)
from bismuth.domain.maintenance import (
    normalise_label,
)
from bismuth.logging_setup import log_trace
from bismuth.ports.vault import INBOX
from bismuth.prompts import subdivision as prompts
from bismuth.services.subdivision.reading import (
    _Contents,
)

logger = logging.getLogger(__name__)


def _normalise(text: str) -> str:
    return "".join(text.split()).casefold()


def _sign(proposed: str, *, axis: str, class_name: str, folder: PurePosixPath) -> str:
    """The note that goes on disk, and a line in the log when it is not the model's.

    The fallback repeats the folder name in other words and rules nothing out, so a run
    that writes it often has a defect worth finding. Without this line the only evidence
    was the shape of the note itself, read off the finished vault by hand.
    """
    if (refusal := sign_refusal(proposed, class_name=class_name)) is not None:
        log_trace(
            "subdivide.sign_refused",
            folder=str(folder),
            name=class_name,
            reason=refusal,
            proposed=proposed[:160],
        )
    return routing_sign(proposed, axis=axis, class_name=class_name)


def _within(candidate: PurePosixPath, root: PurePosixPath) -> bool:
    """Whether ``candidate`` is ``root`` or sits under it."""
    return candidate == root or candidate.parts[: len(root.parts)] == root.parts


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

    wording = " ".join([plan.basis, plan.basis_question] + [group.name for group in plan.groups])
    proposed_script = _writing_system(wording)
    if proposed_script is not None and proposed_script != source_script:
        return "boundary wording uses a different writing system from its documents"
    return None


def _guard_refused(guard: str, *, folder: PurePosixPath, **fields: object) -> None:
    """One line per safety net that fired, under one event name.

    A guard that only refuses is invisible: a run where nothing was built and a run where
    everything was built look the same in a folder count. Named here so a finished run can
    be asked which nets caught what, and how often -- a net that catches on most calls is
    not protecting the design, it is the design.
    """
    log_trace("guard.refused", guard=guard, folder=str(folder), **fields)


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
