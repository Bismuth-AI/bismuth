"""Decides where one document goes, by looking at the folders that exist.

This answers "where in the tree as it stands", and that is all it can answer. It
cannot answer "this folder should now be split" -- that is subdivision, the other
half of filing (SPEC.md 3.4, ADR-0008), and it is not built yet. A folder swelling
with documents that do not belong together is not a failure of judgement here and
will not be fixed by sharpening this prompt.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

from bismuth.domain.document import DocumentCard
from bismuth.domain.paths import sanitize_segment
from bismuth.domain.placement import Placement, Verdict
from bismuth.ports.llm import LLM, ModelProfile
from bismuth.prompts import placement as placement_prompts

logger = logging.getLogger(__name__)


class PlacementService:
    """Answers "where does this go?" by reading the current structure."""

    # Keep in step with Settings.placement_min_confidence, which is what the app passes;
    # this default is what a direct caller (a test, an embedder) gets, and the two drifting
    # apart means they are not testing the shipped behaviour.
    def __init__(self, llm: LLM, *, min_confidence: float = 0.65) -> None:
        self._llm = llm
        self._min_confidence = min_confidence

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
            return Placement.to_inbox(
                document_id,
                reason=decision.reason or "이 문서를 분석하거나 분류할 수 없습니다.",
                confidence=decision.confidence,
            )

        target = _safe_path(decision.folder)
        if target is None:
            logger.warning("placement returned an unusable path %r; parking", decision.folder)
            return Placement.to_inbox(
                document_id,
                reason="제안된 폴더 경로를 쓸 수 없습니다.",
                confidence=decision.confidence,
            )

        if decision.confidence < self._min_confidence:
            # Keep both the number and the folder it named: the user re-deciding this by
            # hand deserves the model's guess, and tuning the threshold needs the figure
            # as a figure rather than as text inside a Korean sentence.
            logger.info(
                "parking %s: model wanted %s at %.0f%%, below the %.0f%% bar",
                document_id,
                target,
                decision.confidence * 100,
                self._min_confidence * 100,
            )
            return Placement.to_inbox(
                document_id,
                reason=f"어디에 둘지 확신이 낮습니다 ({decision.confidence:.0%}): {decision.reason}",
                confidence=decision.confidence,
                suggested=target,
            )

        created = str(target) not in existing_paths
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
