"""Turns extracted text into a card describing what the document is and is about."""

from __future__ import annotations

from bismuth.domain.document import DocumentCard, Extraction
from bismuth.ports.llm import LLM, ModelProfile
from bismuth.prompts import cards as card_prompts


class CardService:
    """Describes documents."""

    def __init__(self, llm: LLM, *, context_chars: int = 12_000) -> None:
        self._llm = llm
        self._context_chars = context_chars

    async def describe(self, extraction: Extraction, *, filename: str) -> DocumentCard:
        """Read a document and say what it is and what it is about."""
        draft = await self._llm.structured(
            card_prompts.build(
                filename=filename,
                text=extraction.head(self._context_chars),
                truncated=extraction.truncated,
            ),
            schema=card_prompts.CardDraft,
            profile=ModelProfile.FAST,
        )
        return DocumentCard(
            title=draft.title.strip() or filename,
            summary=draft.summary.strip() or "(요약 없음)",
            doc_type=draft.doc_type.strip() or "문서",
            topics=tuple(t.strip() for t in draft.topics if t.strip()),
            entities=tuple(draft.entities),
            keywords=tuple(draft.keywords),
            language=draft.language.strip() or "unknown",
            answers_questions=tuple(draft.answers_questions),
        )
