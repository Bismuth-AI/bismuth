"""File one document by walking existing direct-child signs."""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

from bismuth.domain.document import DocumentCard
from bismuth.domain.paths import sanitize_segment
from bismuth.domain.placement import Placement, Verdict
from bismuth.logging_setup import log_context, log_trace
from bismuth.ports.llm import LLM
from bismuth.prompts import placement as placement_prompts

logger = logging.getLogger(__name__)

ROOT = PurePosixPath()


class PlacementService:
    """Answers "where does this go?" one existing tree level at a time."""

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
        """Walk direct child signs until the model stays or reaches a leaf.

        ``existing_paths`` remains an explicit trust boundary supplied by the caller.
        Folder views not present in it are ignored even if an adapter returned them.
        """
        purposes: dict[PurePosixPath, str] = {}
        for raw_path, purpose in folders:
            path = _safe_path(raw_path)
            if path is None or str(path) not in existing_paths:
                continue
            purposes[path] = purpose

        current = ROOT
        steps: list[dict[str, object]] = []
        asked_once = False
        while True:
            direct = sorted(
                ((path, purpose) for path, purpose in purposes.items() if path.parent == current),
                key=lambda item: (item[0].name.casefold(), str(item[0]).casefold()),
            )
            # Once a chosen child is a leaf, there is no decision left to outsource.
            if asked_once and not direct:
                break
            handles = {f"F{index:03d}": path for index, (path, _) in enumerate(direct, start=1)}
            prompt_children = [
                (folder_id, path.name, purpose)
                for folder_id, (path, purpose) in zip(handles, direct, strict=True)
            ]
            offered = [*handles, "STAY", "UNREADABLE"]
            # Placement walks the tree one level at a time, so a call is only
            # interpretable next to the level it was asked at.
            with log_context(stage="placement", window_id=f"placement:{len(steps) + 1:02d}"):
                raw_choice = await self._llm.choose(
                    placement_prompts.build(
                        current=str(current),
                        children=prompt_children,
                        title=card.title,
                        doc_type=card.doc_type,
                        topics=list(card.topics),
                        summary=card.summary,
                        entities=[entity.name for entity in card.entities],
                    ),
                    choices=offered,
                    max_tokens=32,
                    temperature=0.0,
                )
            asked_once = True
            if raw_choice == "UNREADABLE":
                log_trace(
                    "place.decided",
                    document_id=document_id,
                    title=card.title,
                    current=str(current),
                    folders_offered=len(direct),
                    chose=None,
                    verdict=Verdict.INBOX.value,
                )
                return Placement.to_inbox(
                    document_id,
                    reason="document could not be read",
                )

            choice = raw_choice.strip().upper()
            if choice == "STAY":
                steps.append({"at": str(current), "choice": "stay"})
                break
            target = handles.get(choice)
            if target is None:
                # An unknown handle cannot name or escape to any folder. Staying at the
                # current level is the safe interpretation.
                log_trace(
                    "place.invalid_handle",
                    document_id=document_id,
                    current=str(current),
                    asked_for=raw_choice,
                    offered=list(handles),
                )
                steps.append({"at": str(current), "choice": "invalid"})
                break
            steps.append({"at": str(current), "choice": choice, "to": str(target)})
            current = target

        log_trace(
            "place.decided",
            document_id=document_id,
            title=card.title,
            folders_offered=len(purposes),
            chose=str(current),
            root=not current.parts,
            created_folder=False,
            steps=steps,
        )
        return Placement(
            document_id=document_id,
            verdict=Verdict.PLACED,
            target=current,
            created_folder=False,
            rationale="selected through existing folder signs" if current.parts else "kept here",
        )


def _safe_path(raw: str) -> PurePosixPath | None:
    """Turn an adapter path into a vault-safe relative path, or ``None``."""
    segments: list[str] = []
    for part in raw.replace("\\", "/").split("/"):
        part = part.strip()
        if not part or part in (".", ".."):
            continue
        try:
            segments.append(sanitize_segment(part))
        except ValueError:
            continue
    return PurePosixPath(*segments) if segments else None
