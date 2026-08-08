"""Decides where one document goes, by looking at the folders that exist.

This answers "where in the tree as it stands", and that is all it can answer. It
cannot answer "this folder should now be split" -- that is subdivision, the other
half of filing (SPEC.md 3.4, ADR-0008). A folder swelling with documents that do
not belong together is not a failure of judgement here and will not be fixed by
sharpening this prompt.

There is no confidence threshold. "I do not know where this goes" is answered by
the root, which is the folder for documents no distinction has been drawn around
yet; parking such a document instead would be waiting for a person who is not
coming (SPEC.md 3.4).
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

from bismuth.domain.document import DocumentCard
from bismuth.domain.paths import sanitize_segment
from bismuth.domain.placement import Placement, Verdict
from bismuth.logging_setup import log_trace
from bismuth.ports.llm import LLM, ModelProfile
from bismuth.prompts import placement as placement_prompts

logger = logging.getLogger(__name__)

ROOT = PurePosixPath()


class PlacementService:
    """Answers "where does this go?" by reading the current structure."""

    def __init__(self, llm: LLM) -> None:
        self._llm = llm

    async def decide(
        self,
        *,
        document_id: str,
        card: DocumentCard,
        folders: list[tuple[str, str]],
        existing_paths: frozenset[str],
    ) -> Placement:
        """Choose a folder for one document.

        Args:
            folders: (path, purpose) for every folder that exists.
            existing_paths: the same paths as a set, used to tell "reused an
                existing folder" from "created a new one".
        """
        decision = await self._llm.structured(
            placement_prompts.build(
                folders=folders,
                title=card.title,
                doc_type=card.doc_type,
                topics=list(card.topics),
                summary=card.summary,
                entities=[e.name for e in card.entities],
            ),
            schema=placement_prompts.PlacementDecision,
            profile=ModelProfile.REASONING,
        )

        if decision.folder is None:
            # Reserved for documents that could not be read at all. Anything readable
            # has somewhere to go, even if that somewhere is the root.
            log_trace(
                "place.decided",
                document_id=document_id,
                title=card.title,
                folders_offered=len(folders),
                chose=None,
                verdict=Verdict.INBOX.value,
                confidence=decision.confidence,
                reason=decision.reason,
            )
            return Placement.to_inbox(
                document_id,
                reason=decision.reason or "이 문서를 읽을 수 없습니다.",
                confidence=decision.confidence,
            )

        target = _safe_path(decision.folder)
        if target is None and decision.folder.strip():
            # A non-empty path that sanitised away to nothing is a broken answer, not
            # a request for the root.
            logger.warning(
                "placement returned an unusable path %r; using the root", decision.folder
            )
            target = ROOT
        target = target if target is not None else ROOT

        # Placement answers "where in the tree as it stands", so a folder that is not in
        # the tree is not an answer it can give. Categories come from subdivision, which
        # sees several documents before naming anything; here the honest reply is the
        # root (SPEC.md 3.4). Measured without this: twenty invented folders, every one
        # named after a single document, each becoming the precedent for the next.
        invented = bool(target.parts) and str(target) not in existing_paths
        if invented:
            log_trace(
                "place.invented",
                document_id=document_id,
                asked_for=str(target),
                folders_offered=len(folders),
                reason=decision.reason,
            )
            logger.info("placement asked for a folder that does not exist (%s); root", target)
            target = ROOT

        created = False  # only subdivision creates folders
        log_trace(
            "place.decided",
            document_id=document_id,
            title=card.title,
            folders_offered=len(folders),
            asked_for=decision.folder,
            chose=str(target),
            root=not target.parts,
            invented=invented,
            created_folder=created,
            model_said_existing=decision.existing,
            confidence=decision.confidence,
            reason=decision.reason,
        )
        return Placement(
            document_id=document_id,
            verdict=Verdict.PLACED,
            target=target,
            created_folder=created,
            confidence=decision.confidence,
            rationale=decision.reason,
        )


def _safe_path(raw: str) -> PurePosixPath | None:
    """Turn the model's ``a/b/c`` string into a vault-safe relative path, or None."""
    segments: list[str] = []
    for part in raw.replace("\\", "/").split("/"):
        part = part.strip()
        if not part or part in (".", ".."):
            continue
        try:
            segments.append(sanitize_segment(part))
        except ValueError:
            continue
    if not segments:
        return None
    return PurePosixPath(*segments)
