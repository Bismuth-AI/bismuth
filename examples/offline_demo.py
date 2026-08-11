"""Run the whole engine with no API key, no network, and no cost.

    python examples/offline_demo.py

Not a mock -- the real filesystem, journal, placement rules, sidecar and folder
notes. Only the model is scripted (:class:`FakeLLM`) so the run is free and
identical every time. Also the shortest possible example of embedding the engine:
``build(settings, llm=...)`` is the whole integration surface.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import anyio
from pydantic import BaseModel

from bismuth.adapters.llm.fake import FakeLLM
from bismuth.config import Settings
from bismuth.container import build
from bismuth.domain.document import Entity, EntityKind
from bismuth.ports.llm import Prompt
from bismuth.prompts import cards as card_prompts
from bismuth.prompts import charters as charter_prompts
from bismuth.prompts import placement as placement_prompts

HERE = Path(__file__).parent
VAULT = HERE / "demo-vault"

DOCUMENTS = {
    "아폴로_계약서_2023.txt": "아폴로 사업 유지보수 계약서. 발주자 대한물산, 수급자 유엔진. 2023년.",
    "아폴로_회의록_2023.txt": "아폴로 사업 킥오프 회의록. 참석 대한물산, 유엔진. 2023년.",
    "제피르_제안서_2024.txt": "제피르 차세대 플랫폼 제안서. 제출처 한빛전자. 2024년.",
}


def scripted(prompt: Prompt, schema: type[BaseModel]) -> BaseModel:
    """What a model would return. Keyed off a string unique to each document."""
    u = prompt.user
    zephyr = "한빛전자" in u or "제피르" in u

    if schema is card_prompts.CardDraft:
        if zephyr:
            return card_prompts.CardDraft(
                title="제피르 차세대 플랫폼 제안서",
                summary="한빛전자 ERP를 전환하는 제피르 제안서.",
                doc_type="제안서",
                language="ko",
                topics=["제피르", "한빛전자", "2024"],
                entities=[Entity(name="한빛전자", kind=EntityKind.ORGANIZATION)],
                keywords=["ERP"],
                answers_questions=["제피르 제안 금액은?"],
            )
        return card_prompts.CardDraft(
            title="아폴로 사업 문서",
            summary="대한물산과 유엔진의 아폴로 사업 문서.",
            doc_type="계약서" if "계약" in u else "회의록",
            language="ko",
            topics=["아폴로", "대한물산", "2023"],
            entities=[Entity(name="대한물산", kind=EntityKind.ORGANIZATION)],
            keywords=["아폴로"],
            answers_questions=["아폴로 사업에서 무엇이 합의되었나?"],
        )
    if schema is placement_prompts.PlacementDecision:
        return placement_prompts.PlacementDecision(folder_id="F001", confidence=0.9)
    if schema is charter_prompts.CharterDraft:
        name = "제피르 2024" if zephyr else "아폴로 2023"
        return charter_prompts.CharterDraft(
            title=name,
            purpose=f"{name} 문서를 모아둡니다.",
            holds=["계약서·제안서·보고서·회의록"],
            answers=["무엇이 합의되었나?"],
        )
    raise AssertionError(f"nothing scripted for {schema.__name__}")


async def run() -> None:
    if VAULT.exists():
        shutil.rmtree(VAULT)
    engine = build(Settings(vault_path=VAULT), llm=FakeLLM(handler=scripted))

    print(f"vault: {VAULT}\n")
    for filename, body in DOCUMENTS.items():
        rel = engine.ingest.stage(body.encode("utf-8"), filename)
        result = await engine.ingest.process(rel)
        tag = "새 폴더" if result.placement.created_folder else "기존 폴더"
        print(f"  {filename}  →  {result.destination}/  [{tag}]")

    print("\n디스크에 남은 것:\n")
    for path in sorted(VAULT.rglob("*")):
        if ".bismuth" in path.parts or path.is_dir() or path.name == "_inbox":
            continue
        print(f"  {path.relative_to(VAULT).as_posix()}")

    print("\n모든 변경은 되돌릴 수 있습니다:\n")
    for entry in engine.journal.iter_entries(limit=3):
        print(f"  {entry.id}  {entry.reason}")


if __name__ == "__main__":
    anyio.run(run)
