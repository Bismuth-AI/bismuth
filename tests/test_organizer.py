"""The organizer submits a complete shadow plan before autonomous application."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import yaml
from agentkit.messages import AssistantMessage
from agentkit.testing import FakeModel, call, says
from fastapi.testclient import TestClient

from bismuth.api.app import create_app
from bismuth.config import Settings
from bismuth.container import Bismuth, build
from bismuth.domain.charter import CHARTER_FILENAME, Charter
from bismuth.services.agent import (
    AgentService,
    ProposedBoundary,
    ProposedMove,
    _document_handles,
    _finding_signature,
    _SubmitPlanArgs,
    _validate_shadow_plan,
    build_submit_plan_tool,
)
from bismuth.services.maintenance_windows import family_components
from bismuth.services.organizer.planning import _ReviewOutcome
from tests.conftest import seed_folder
from tests.test_ingest import add


def test_placement_finding_clears_when_cited_documents_change_target() -> None:
    handles = {
        "D000001": PurePosixPath("a.pdf"),
        "D000002": PurePosixPath("b.pdf"),
    }
    original = [
        ProposedBoundary(
            parent="",
            operation="create_boundary",
            axis="topic",
            axis_question="Which topic?",
            moves=[ProposedMove(paths=["a.pdf", "b.pdf"], target="Finance")],
        )
    ]
    revised = [
        ProposedBoundary(
            parent="",
            operation="create_boundary",
            axis="topic",
            axis_question="Which topic?",
            moves=[ProposedMove(paths=["a.pdf", "b.pdf"], target="Fair Trade")],
        )
    ]

    assert _finding_signature(
        original,
        ["D000001", "D000002"],
        handles=handles,
        kind="mixed_axis",
    ) != _finding_signature(
        revised,
        ["D000001", "D000002"],
        handles=handles,
        kind="mixed_axis",
    )


def _svc(engine: Bismuth, model: FakeModel) -> AgentService:
    return AgentService(
        model=model,
        vault=engine.vault,
        charters=engine.charters,
        catalog=engine.catalog,
    )


def _plan_call() -> object:
    return call(
        "submit_plan",
        {
            "boundaries": [
                {
                    "parent": "",
                    "operation": "replace_boundary",
                    "axis": "문서 성격",
                    "axis_question": "이 문서는 어떤 성격인가?",
                    "moves": [
                        {
                            "document_ids": ["D000001", "D000002"],
                            "target": "계약",
                        },
                        {
                            "document_ids": ["D000003", "D000004"],
                            "target": "보고",
                        },
                    ],
                }
            ]
        },
    )


def _accept_review() -> object:
    return call("submit_review", {"findings": []})


def _accepted_plan_turns() -> list[AssistantMessage]:
    return [
        says("탐색 증거를 수집했습니다."),
        says("완성안을 제출합니다", _plan_call()),
        says("경계 증거 수집 완료"),
        says("경계 후보를 검토했습니다", _accept_review()),
        says("membership 증거 수집 완료"),
        says("membership을 검토했습니다", _accept_review()),
    ]


async def _four_documents(engine: Bismuth) -> None:
    for index, name in enumerate(("a.txt", "b.txt", "c.txt", "d.txt"), start=1):
        await add(engine, name, f"서로 다른 문서 {index}")


async def test_semantic_reviewer_failure_rolls_back_the_candidate_fingerprint(
    engine: Bismuth,
) -> None:
    await _four_documents(engine)
    accepted: list[ProposedBoundary] = []
    problems: list[str] = []
    attempts = 0

    async def reviewer(_: list[ProposedBoundary]) -> _ReviewOutcome:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("critic context reservation failed")
        return _ReviewOutcome()

    tool = build_submit_plan_tool(
        engine.vault,
        scope=PurePosixPath(),
        handles=_document_handles(engine.vault),
        sink=accepted,
        problem_sink=problems,
        semantic_reviewer=reviewer,
    )
    args = tool.params.model_validate(_plan_call().arguments)  # type: ignore[attr-defined]

    first = await tool.run(args)  # type: ignore[attr-defined]
    second = await tool.run(args)  # type: ignore[attr-defined]

    assert "fingerprint was rolled back" in first
    assert second.startswith("Shadow plan accepted:")
    assert attempts == 2


async def test_candidate_evidence_distinguishes_existing_and_new_targets(
    engine: Bismuth,
) -> None:
    await _four_documents(engine)
    (Path(engine.vault.root) / "기존분류").mkdir()
    handles = _document_handles(engine.vault)
    paths = list(handles.values())
    boundaries = [
        ProposedBoundary(
            parent="",
            operation="add_sibling",
            axis="문서 주제",
            axis_question="이 문서의 주제는 무엇인가?",
            moves=[
                ProposedMove(paths=[str(paths[0])], target="기존분류"),
                ProposedMove(paths=[str(paths[1])], target="신규분류"),
            ],
        )
    ]
    service = _svc(engine, FakeModel([says("unused")]))

    evidence = service._candidate_evidence(boundaries, handles=handles)

    assert '"target_state": "existing_target"' in evidence
    assert '"target_state": "new_target"' in evidence
    assert "PROPOSED EXISTING_TARGET 기존분류" in evidence
    assert "PROPOSED NEW_TARGET 신규분류" in evidence
    assert service._duplicate_finding_only_names_existing_targets(["기존분류"], boundaries)
    assert not service._duplicate_finding_only_names_existing_targets(["신규분류"], boundaries)


def _rewrite_sidecar_identity(path: Path, *, title: str, source: str) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    end = next(index for index in range(1, len(lines)) if lines[index] == "---")
    meta = yaml.safe_load("\n".join(lines[1:end]))
    meta["title"] = title
    meta["source"] = source
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).rstrip()
    path.write_text(f"---\n{front}\n---\n" + "\n".join(lines[end + 1 :]), encoding="utf-8")


async def test_korean_collection_rejects_english_boundary_language(engine: Bismuth) -> None:
    await _four_documents(engine)
    handles = _document_handles(engine.vault)
    ids = list(handles)
    args = _SubmitPlanArgs.model_validate(
        {
            "boundaries": [
                {
                    "parent": "아폴로/2023",
                    "operation": "create_boundary",
                    "axis": "document_type",
                    "axis_question": "What type is this document?",
                    "moves": [
                        {"document_ids": ids[:2], "target": "Contracts"},
                        {"document_ids": ids[2:], "target": "Reports"},
                    ],
                }
            ]
        }
    )

    _, problems = _validate_shadow_plan(
        engine.vault,
        args,
        scope=PurePosixPath("아폴로/2023"),
        handles=handles,
    )

    assert any("dominant writing system" in problem for problem in problems)


async def test_explicit_family_split_is_rejected_without_rewriting_the_plan(
    engine: Bismuth,
) -> None:
    await _four_documents(engine)
    parent = Path(engine.vault.root) / "아폴로/2023"
    _rewrite_sidecar_identity(
        parent / "a.txt.md",
        title="기술보증기금법",
        source="기술보증기금법.txt",
    )
    _rewrite_sidecar_identity(
        parent / "b.txt.md",
        title="기술보증기금법 시행령",
        source="기술보증기금법 시행령.txt",
    )
    handles = _document_handles(engine.vault)
    by_name = {path.name: handle for handle, path in handles.items()}
    args = _SubmitPlanArgs.model_validate(
        {
            "boundaries": [
                {
                    "parent": "아폴로/2023",
                    "operation": "create_boundary",
                    "axis": "문서 주제",
                    "axis_question": "이 문서의 주제는 무엇인가?",
                    "moves": [
                        {
                            "document_ids": [by_name["a.txt"]],
                            "target": "금융",
                        },
                        {
                            "document_ids": [
                                by_name["b.txt"],
                                by_name["c.txt"],
                                by_name["d.txt"],
                            ],
                            "target": "기술",
                        },
                    ],
                }
            ]
        }
    )

    boundaries, problems = _validate_shadow_plan(
        engine.vault,
        args,
        scope=PurePosixPath("아폴로/2023"),
        handles=handles,
    )

    assert boundaries
    assert any("assigned to different targets" in problem for problem in problems)
    finance = next(move for move in boundaries[0].moves if move.target.endswith("/금융"))
    technology = next(move for move in boundaries[0].moves if move.target.endswith("/기술"))
    assert {PurePosixPath(path).name for path in finance.paths} == {"a.txt"}
    assert {PurePosixPath(path).name for path in technology.paths} >= {"b.txt"}


async def test_partial_family_membership_is_rejected_without_implicit_closure(
    engine: Bismuth,
) -> None:
    await _four_documents(engine)
    parent = Path(engine.vault.root) / "아폴로/2023"
    _rewrite_sidecar_identity(
        parent / "a.txt.md",
        title="과학관설립운영법",
        source="과학관설립운영법.txt",
    )
    _rewrite_sidecar_identity(
        parent / "b.txt.md",
        title="과학관설립운영법 시행령",
        source="과학관설립운영법 시행령.txt",
    )
    handles = _document_handles(engine.vault)
    by_name = {path.name: handle for handle, path in handles.items()}
    args = _SubmitPlanArgs.model_validate(
        {
            "boundaries": [
                {
                    "parent": "아폴로/2023",
                    "operation": "create_boundary",
                    "axis": "문서 주제",
                    "axis_question": "이 문서의 주제는 무엇인가?",
                    "moves": [
                        {
                            "document_ids": [by_name["a.txt"]],
                            "target": "과학",
                        },
                        {
                            "document_ids": [by_name["c.txt"], by_name["d.txt"]],
                            "target": "기타주제",
                        },
                    ],
                }
            ]
        }
    )

    boundaries, problems = _validate_shadow_plan(
        engine.vault,
        args,
        scope=PurePosixPath("아폴로/2023"),
        handles=handles,
    )

    family_problem = next(
        problem for problem in problems if "document family would be split" in problem
    )
    assert by_name["a.txt"] in family_problem
    assert by_name["b.txt"] in family_problem
    assert "current=" in family_problem
    assert "final=" in family_problem
    science = next(move for move in boundaries[0].moves if move.target.endswith("/과학"))
    assert {PurePosixPath(path).name for path in science.paths} == {"a.txt"}


async def test_family_unit_is_the_only_assignable_handle(engine: Bismuth) -> None:
    await _four_documents(engine)
    handles = _document_handles(engine.vault)
    by_name = {path.name: handle for handle, path in handles.items()}
    family_units = {"F001": (by_name["a.txt"], by_name["b.txt"])}
    args = _SubmitPlanArgs.model_validate(
        {
            "boundaries": [
                {
                    "parent": "아폴로/2023",
                    "operation": "create_boundary",
                    "axis": "문서 주제",
                    "axis_question": "이 문서의 주제는 무엇인가?",
                    "moves": [
                        {"document_ids": ["F001"], "target": "계약"},
                        {
                            "document_ids": [by_name["c.txt"], by_name["d.txt"]],
                            "target": "보고",
                        },
                    ],
                }
            ]
        }
    )

    boundaries, problems = _validate_shadow_plan(
        engine.vault,
        args,
        scope=PurePosixPath("아폴로/2023"),
        handles=handles,
        family_units=family_units,
        require_family_units=True,
    )

    assert problems == []
    contract = next(move for move in boundaries[0].moves if move.target.endswith("/계약"))
    assert {PurePosixPath(path).name for path in contract.paths} == {"a.txt", "b.txt"}

    args.boundaries[0].moves[0].document_ids = [by_name["a.txt"]]
    _, split_problems = _validate_shadow_plan(
        engine.vault,
        args,
        scope=PurePosixPath("아폴로/2023"),
        handles=handles,
        family_units=family_units,
        require_family_units=True,
    )
    assert any("must be assigned with indivisible unit F001" in item for item in split_problems)


async def test_rejected_candidate_then_prose_is_a_safe_no_change(engine: Bismuth) -> None:
    await _four_documents(engine)
    parent = Path(engine.vault.root) / "아폴로/2023"
    _rewrite_sidecar_identity(
        parent / "a.txt.md",
        title="과학관설립운영법",
        source="과학관설립운영법.txt",
    )
    _rewrite_sidecar_identity(
        parent / "b.txt.md",
        title="과학관설립운영법 시행령",
        source="과학관설립운영법 시행령.txt",
    )
    handles = _document_handles(engine.vault)
    by_name = {path.name: handle for handle, path in handles.items()}
    rejected = call(
        "submit_plan",
        {
            "boundaries": [
                {
                    "parent": "아폴로/2023",
                    "operation": "create_boundary",
                    "axis": "문서 주제",
                    "axis_question": "이 문서의 주제는 무엇인가?",
                    "moves": [
                        {"document_ids": [by_name["a.txt"], by_name["c.txt"]], "target": "과학"},
                        {"document_ids": [by_name["b.txt"], by_name["d.txt"]], "target": "행정"},
                    ],
                }
            ]
        },
    )
    model = FakeModel(
        [
            says("", rejected),
            says("No valid structure remains."),
            says(
                "",
                call(
                    "finish_no_change",
                    {"reason": "No valid structure remains after deterministic validation."},
                ),
            ),
        ]
    )

    proposal = await _svc(engine, model).propose_reorg(scope="아폴로/2023")

    assert proposal.moves == []
    assert proposal.problems == []
    assert "No valid structure remains" in proposal.summary


async def test_shadow_plan_is_validated_without_touching_disk(engine: Bismuth) -> None:
    await _four_documents(engine)
    model = FakeModel(_accepted_plan_turns())

    proposal = await _svc(engine, model).propose_reorg()

    assert [(move.paths, move.target) for move in proposal.moves] == [
        (["아폴로/2023/a.txt", "아폴로/2023/b.txt"], "계약"),
        (["아폴로/2023/c.txt", "아폴로/2023/d.txt"], "보고"),
    ]
    assert proposal.problems == []
    assert proposal.summary == ("검증된 구조 계획\n/ [replace_boundary] — 계약 2개, 보고 2개")
    assert (engine.vault.root / "아폴로/2023/a.txt").is_file()
    assert not (engine.vault.root / "아폴로/2023/계약").exists()


async def test_propose_can_recommend_no_change(engine: Bismuth) -> None:
    await add(engine, "a.txt", "아폴로 계약 A")
    model = FakeModel(
        [
            says("", call("tree", {})),
            says("탐색을 마쳤습니다."),
            says(
                "",
                call(
                    "finish_no_change",
                    {"reason": "구조가 이미 명확합니다. 바꿀 것이 없습니다."},
                ),
            ),
        ]
    )

    proposal = await _svc(engine, model).propose_reorg()

    assert proposal.moves == []
    assert "바꿀 것이 없습니다" in proposal.summary


async def test_planner_early_prose_final_gets_one_required_tool_retry(engine: Bismuth) -> None:
    await add(engine, "a.txt", "single document")
    model = FakeModel(
        [
            says("I inspected the vault and would keep the current structure."),
            says(
                "",
                call(
                    "finish_no_change",
                    {"reason": "There is only one document, so no reusable boundary is justified."},
                ),
            ),
            says("done"),
        ]
    )

    proposal = await _svc(engine, model).propose_reorg()

    assert proposal.problems == []
    assert "only one document" in proposal.summary


async def test_finish_exploration_is_a_hard_phase_boundary(engine: Bismuth) -> None:
    await add(engine, "single.txt", "하나뿐인 문서")
    model = FakeModel(
        [
            says(
                "",
                call(
                    "finish_exploration",
                    {"summary": "루트에 단일 문서만 있어 구조 근거가 부족합니다."},
                ),
            ),
            says(
                "",
                call(
                    "finish_no_change",
                    {"reason": "단일 문서만으로 재사용 가능한 형제 경계를 만들 수 없습니다."},
                ),
            ),
        ]
    )

    proposal = await _svc(engine, model).propose_reorg()

    assert proposal.problems == []
    assert len(model.calls) == 2
    assert model.calls[0][0] != model.calls[1][0]


async def test_no_change_clears_an_abandoned_rejected_draft(engine: Bismuth) -> None:
    await add(engine, "a.txt", "아폴로 계약 A")
    model = FakeModel(
        [
            says("탐색을 마쳤습니다."),
            says("", call("submit_plan", {"boundaries": []})),
            says(
                "",
                call(
                    "finish_no_change",
                    {"reason": "거절된 초안보다 현재 구조를 보존하는 편이 안전합니다."},
                ),
            ),
        ]
    )

    proposal = await _svc(engine, model).propose_reorg()

    assert proposal.problems == []
    assert "보존" in proposal.summary


async def test_exact_candidate_semantic_finding_blocks_the_plan(engine: Bismuth) -> None:
    await _four_documents(engine)
    model = FakeModel(
        [
            says("완성안을 제출합니다", _plan_call()),
            says(
                "포함관계를 찾았습니다",
                call(
                    "submit_review",
                    {
                        "findings": [
                            {
                                "kind": "level_mismatch",
                                "subjects": ["계약", "보고"],
                                "evidence_handles": ["D000001", "D000003"],
                                "instruction": "같은 추상화 수준의 형제 경계를 다시 제안하세요.",
                                "blocking": True,
                            }
                        ],
                    },
                ),
            ),
            says("경계 검토 완료"),
            says("membership을 검토했습니다", _accept_review()),
            says("membership 검토 완료"),
            says(
                "",
                call(
                    "finish_no_change",
                    {"reason": "의미 반례를 해소할 안전한 대체 경계를 찾지 못했습니다."},
                ),
            ),
            says("현재 구조를 보존합니다."),
        ]
    )

    proposal = await _svc(engine, model).propose_reorg()

    assert proposal.moves == []
    assert proposal.problems == []
    assert "찾지 못했습니다" in proposal.summary


async def test_create_boundary_keeps_uncited_siblings_after_one_target_is_blocked(
    engine: Bismuth,
) -> None:
    for index, name in enumerate(("a.txt", "b.txt", "c.txt", "d.txt", "e.txt", "f.txt")):
        await add(engine, name, f"서로 다른 문서 {index}")
    handles = _document_handles(engine.vault)
    by_name = {path.name: handle for handle, path in handles.items()}
    candidate = call(
        "submit_plan",
        {
            "boundaries": [
                {
                    "parent": "아폴로/2023",
                    "operation": "create_boundary",
                    "axis": "문서 분야",
                    "axis_question": "이 문서의 분야는 무엇인가?",
                    "moves": [
                        {
                            "document_ids": [by_name["a.txt"], by_name["b.txt"]],
                            "target": "검토대상",
                        },
                        {
                            "document_ids": [by_name["c.txt"], by_name["d.txt"]],
                            "target": "계약",
                        },
                        {
                            "document_ids": [by_name["e.txt"], by_name["f.txt"]],
                            "target": "보고",
                        },
                    ],
                }
            ]
        },
    )
    finding = call(
        "submit_review",
        {
            "findings": [
                {
                    "kind": "level_mismatch",
                    "subjects": ["검토대상"],
                    "evidence_handles": [by_name["a.txt"], by_name["b.txt"]],
                    "instruction": "검토대상 문서만 다른 추상화 수준입니다.",
                    "blocking": True,
                }
            ]
        },
    )
    model = FakeModel(
        [
            says("탐색 완료"),
            says("", candidate),
            says("경계 증거 완료"),
            says("", finding),
            says("membership 증거 완료"),
            says("", _accept_review()),
        ]
    )

    proposal = await _svc(engine, model).propose_reorg(scope="아폴로/2023")

    assert proposal.problems == []
    assert {move.target.rsplit("/", 1)[-1] for move in proposal.moves} == {"계약", "보고"}


async def test_critic_cannot_reject_an_exact_grounded_family_partition(
    engine: Bismuth,
) -> None:
    await _four_documents(engine)
    parent = Path(engine.vault.root) / "아폴로/2023"
    for filename, title in (
        ("a.txt.md", "과학관설립운영법"),
        ("b.txt.md", "과학관설립운영법 시행령"),
        ("c.txt.md", "기술지도지원법"),
        ("d.txt.md", "기술지도지원법 시행령"),
    ):
        _rewrite_sidecar_identity(
            parent / filename,
            title=title,
            source=f"{title}.txt",
        )
    handles = _document_handles(engine.vault)
    by_name = {path.name: handle for handle, path in handles.items()}
    candidate = call(
        "submit_plan",
        {
            "boundaries": [
                {
                    "parent": "아폴로/2023",
                    "operation": "create_boundary",
                    "axis": "문서 계열",
                    "axis_question": "이 문서가 속한 문서 계열은 무엇인가?",
                    "moves": [
                        {
                            "document_ids": [by_name["a.txt"], by_name["b.txt"]],
                            "target": "과학관법",
                        },
                        {
                            "document_ids": [by_name["c.txt"], by_name["d.txt"]],
                            "target": "기술사법",
                        },
                    ],
                }
            ]
        },
    )
    hostile = call(
        "submit_review",
        {
            "findings": [
                {
                    "kind": "mixed_axis",
                    "subjects": ["과학관법", "기술사법"],
                    "evidence_handles": list(by_name.values()),
                    "instruction": "한 계열은 법률과 시행령이고 다른 계열은 판본 관계입니다.",
                    "blocking": True,
                }
            ]
        },
    )
    forced = call(
        "submit_review",
        {
            "findings": [
                {
                    "kind": "forced_fit",
                    "subjects": ["과학관법"],
                    "evidence_handles": [by_name["a.txt"], by_name["b.txt"]],
                    "instruction": "서로 다른 법령 유형이므로 함께 두면 안 됩니다.",
                    "blocking": True,
                }
            ]
        },
    )
    model = FakeModel(
        [
            says("탐색 완료"),
            says("", candidate),
            says("경계 증거 완료"),
            says("", hostile),
            says("membership 증거 완료"),
            says("", forced),
        ]
    )

    proposal = await _svc(engine, model).propose_reorg(scope="아폴로/2023")

    assert proposal.problems == []
    assert {move.target.rsplit("/", 1)[-1] for move in proposal.moves} == {
        "과학관법",
        "기술사법",
    }


async def test_membership_critic_cannot_misuse_duplicate_boundary_for_documents(
    engine: Bismuth,
) -> None:
    await _four_documents(engine)
    model = FakeModel(
        [
            says("탐색 완료"),
            says("", _plan_call()),
            says("경계 증거 완료"),
            says("", _accept_review()),
            says("membership 증거 완료"),
            says(
                "",
                call(
                    "submit_review",
                    {
                        "findings": [
                            {
                                "kind": "duplicate_boundary",
                                "subjects": ["D000001", "D000002"],
                                "evidence_handles": ["D000001", "D000002"],
                                "instruction": "These two documents look like duplicate copies.",
                                "blocking": True,
                            }
                        ]
                    },
                ),
            ),
        ]
    )

    proposal = await _svc(engine, model).propose_reorg()

    assert proposal.problems == []
    assert proposal.boundaries


async def test_candidate_limit_is_enforced_by_the_host(engine: Bismuth) -> None:
    await _four_documents(engine)
    finding_one = call(
        "submit_review",
        {
            "findings": [
                {
                    "kind": "level_mismatch",
                    "subjects": ["D000001"],
                    "evidence_handles": ["D000001"],
                    "instruction": "D000001의 배치를 다시 검토하세요.",
                    "blocking": True,
                }
            ]
        },
    )
    revised = call(
        "submit_plan",
        {
            "boundaries": [
                {
                    "parent": "",
                    "operation": "replace_boundary",
                    "axis": "문서 성격",
                    "axis_question": "이 문서는 어떤 성격인가?",
                    "moves": [
                        {"document_ids": ["D000002", "D000004"], "target": "계약"},
                        {"document_ids": ["D000001", "D000003"], "target": "보고"},
                    ],
                }
            ]
        },
    )
    finding_two = call(
        "submit_review",
        {
            "findings": [
                {
                    "kind": "level_mismatch",
                    "subjects": ["D000002"],
                    "evidence_handles": ["D000002"],
                    "instruction": "D000002의 배치를 다시 검토하세요.",
                    "blocking": True,
                }
            ]
        },
    )
    third_candidate = call(
        "submit_plan",
        {
            "boundaries": [
                {
                    "parent": "",
                    "operation": "replace_boundary",
                    "axis": "문서 성격",
                    "axis_question": "이 문서는 어떤 성격인가?",
                    "moves": [
                        {"document_ids": ["D000001", "D000004"], "target": "계약"},
                        {"document_ids": ["D000002", "D000003"], "target": "보고"},
                    ],
                }
            ]
        },
    )
    model = FakeModel(
        [
            says("탐색 완료"),
            says("", third_candidate),
            says("경계 증거 완료"),
            says("", finding_one),
            says("membership 증거 완료"),
            says("", _accept_review()),
            says("", revised),
            says("경계 증거 완료"),
            says("", finding_two),
            says("membership 증거 완료"),
            says("", _accept_review()),
            says("", _plan_call()),
            says(
                "",
                call(
                    "finish_no_change",
                    {"reason": "두 후보가 모두 거절되어 현재 구조를 안전하게 보존합니다."},
                ),
            ),
        ]
    )

    proposal = await _svc(engine, model).propose_reorg()

    assert proposal.moves == []
    assert proposal.problems == []
    assert "두 후보" in proposal.summary
    tool_results = [
        message.content
        for _, messages, _ in model.calls
        for message in messages
        if message.role == "tool"
    ]
    assert any(
        "Candidate limit reached (2 reviewed candidates" in result for result in tool_results
    )


async def test_revised_candidate_must_change_cited_blocking_placements(
    engine: Bismuth,
) -> None:
    await _four_documents(engine)
    revised = call(
        "submit_plan",
        {
            "boundaries": [
                {
                    "parent": "",
                    "operation": "replace_boundary",
                    "axis": "문서 성격",
                    "axis_question": "이 문서는 어떤 성격인가?",
                    "moves": [
                        {"document_ids": ["D000001", "D000004"], "target": "계약"},
                        {"document_ids": ["D000002", "D000003"], "target": "보고"},
                    ],
                }
            ]
        },
    )
    finding = call(
        "submit_review",
        {
            "findings": [
                {
                    "kind": "level_mismatch",
                    "subjects": ["계약", "보고"],
                    "evidence_handles": ["D000001", "D000003"],
                    "instruction": "인용된 두 문서의 형제 배치를 다시 검토하세요.",
                    "blocking": True,
                }
            ]
        },
    )
    model = FakeModel(
        [
            says("탐색 완료"),
            says("", _plan_call()),
            says("경계 증거 완료"),
            says("", finding),
            says("membership 증거 완료"),
            says("", _accept_review()),
            says("", revised),
            says(
                "",
                call(
                    "finish_no_change",
                    {"reason": "인용된 blocking 배치를 바꾸지 못해 현재 구조를 보존합니다."},
                ),
            ),
        ]
    )

    proposal = await _svc(engine, model).propose_reorg()

    assert proposal.moves == []
    tool_results = [
        message.content
        for _, messages, _ in model.calls
        for message in messages
        if message.role == "tool"
    ]
    assert any("leaves a prior blocking finding unchanged" in result for result in tool_results)


async def test_route_existing_cannot_create_or_rewrite_a_boundary(engine: Bismuth) -> None:
    await _four_documents(engine)
    parent = Path(engine.vault.root) / "아폴로/2023"
    (parent / "계약").mkdir()
    handles = _document_handles(engine.vault)
    args = _SubmitPlanArgs.model_validate(
        {
            "boundaries": [
                {
                    "parent": "아폴로/2023",
                    "operation": "route_existing",
                    "moves": [
                        {"document_ids": list(handles)[:2], "target": "계약"},
                    ],
                }
            ]
        }
    )

    boundaries, problems = _validate_shadow_plan(
        engine.vault,
        args,
        scope=PurePosixPath("아폴로/2023"),
        handles=handles,
    )

    assert problems == []
    assert boundaries[0].operation == "route_existing"
    assert boundaries[0].axis == ""


async def test_rehome_existing_repairs_a_document_between_existing_siblings(
    engine: Bismuth,
) -> None:
    await _four_documents(engine)
    parent = Path(engine.vault.root) / "아폴로/2023"
    contract = parent / "계약"
    report = parent / "보고"
    contract.mkdir()
    report.mkdir()
    (parent / CHARTER_FILENAME).write_text(
        Charter(
            path=PurePosixPath("아폴로/2023"),
            title="2023",
            purpose="",
            split_basis="문서 성격",
            split_question="이 문서는 어떤 성격인가?",
            split_at_documents=4,
        ).to_markdown(),
        encoding="utf-8",
    )
    for folder in (contract, report):
        (folder / CHARTER_FILENAME).write_text(
            Charter(
                path=PurePosixPath(f"아폴로/2023/{folder.name}"),
                title=folder.name,
                purpose=f"기존 표지판 {folder.name}",
                boundary_basis="문서 성격",
                boundary_question="이 문서는 어떤 성격인가?",
                boundary_answer=folder.name,
            ).to_markdown(),
            encoding="utf-8",
        )
    for name, target in (("a.txt", contract), ("b.txt", contract), ("c.txt", report)):
        (parent / name).replace(target / name)
        (parent / f"{name}.md").replace(target / f"{name}.md")
    handles = _document_handles(engine.vault)
    document_id = next(handle for handle, path in handles.items() if path.name == "a.txt")
    args = _SubmitPlanArgs.model_validate(
        {
            "boundaries": [
                {
                    "parent": "아폴로/2023",
                    "operation": "rehome_existing",
                    "moves": [{"document_ids": [document_id], "target": "보고"}],
                }
            ]
        }
    )

    boundaries, problems = _validate_shadow_plan(
        engine.vault,
        args,
        scope=PurePosixPath("아폴로/2023"),
        handles=handles,
    )

    assert problems == []
    assert boundaries[0].operation == "rehome_existing"
    assert boundaries[0].moves[0].paths == ["아폴로/2023/계약/a.txt"]
    assert boundaries[0].moves[0].target == "아폴로/2023/보고"
    notes_before = {
        folder.name: (folder / CHARTER_FILENAME).read_text(encoding="utf-8")
        for folder in (contract, report)
    }

    moved = engine.agent._apply_boundaries(boundaries)

    assert moved == 1
    assert not (contract / "a.txt").exists()
    assert not (contract / "a.txt.md").exists()
    assert (report / "a.txt").is_file()
    assert (report / "a.txt.md").is_file()
    assert notes_before == {
        folder.name: (folder / CHARTER_FILENAME).read_text(encoding="utf-8")
        for folder in (contract, report)
    }


async def test_rehome_existing_cannot_silently_remove_a_boundary_value(
    engine: Bismuth,
) -> None:
    await _four_documents(engine)
    parent = Path(engine.vault.root) / "아폴로/2023"
    contract = parent / "계약"
    report = parent / "보고"
    contract.mkdir()
    report.mkdir()
    (parent / "a.txt").replace(contract / "a.txt")
    (parent / "a.txt.md").replace(contract / "a.txt.md")
    handles = _document_handles(engine.vault)
    document_id = next(handle for handle, path in handles.items() if path.name == "a.txt")
    args = _SubmitPlanArgs.model_validate(
        {
            "boundaries": [
                {
                    "parent": "아폴로/2023",
                    "operation": "rehome_existing",
                    "moves": [{"document_ids": [document_id], "target": "보고"}],
                }
            ]
        }
    )

    _, problems = _validate_shadow_plan(
        engine.vault,
        args,
        scope=PurePosixPath("아폴로/2023"),
        handles=handles,
    )

    assert any("would empty existing boundary values" in problem for problem in problems)


async def test_local_organizer_cannot_submit_another_scope(engine: Bismuth) -> None:
    await _four_documents(engine)
    handles = _document_handles(engine.vault)
    args = _SubmitPlanArgs.model_validate(
        {
            "boundaries": [
                {
                    "parent": "",
                    "operation": "create_boundary",
                    "axis": "문서 성격",
                    "axis_question": "이 문서는 어떤 성격인가?",
                    "moves": [
                        {"document_ids": list(handles)[:2], "target": "계약"},
                        {"document_ids": list(handles)[2:], "target": "보고"},
                    ],
                }
            ]
        }
    )

    _, problems = _validate_shadow_plan(
        engine.vault,
        args,
        scope=PurePosixPath("아폴로/2023"),
        handles=handles,
    )

    assert any("must equal the assigned scope" in problem for problem in problems)


async def test_existing_siblings_count_when_adding_one_new_value(engine: Bismuth) -> None:
    await _four_documents(engine)
    parent = Path(engine.vault.root) / "아폴로/2023"
    (parent / "기존분류").mkdir()
    (parent / CHARTER_FILENAME).write_text(
        Charter(
            path=PurePosixPath("아폴로/2023"),
            title="2023",
            purpose="",
            split_basis="문서 주제",
            split_question="이 문서의 주제는 무엇인가?",
            split_at_documents=4,
        ).to_markdown(),
        encoding="utf-8",
    )
    handles = _document_handles(engine.vault)
    ids = list(handles)[:2]
    args = _SubmitPlanArgs.model_validate(
        {
            "boundaries": [
                {
                    "parent": "아폴로/2023",
                    "operation": "add_sibling",
                    "moves": [
                        {
                            "document_ids": ids,
                            "target": "아폴로/2023/새분류",
                        }
                    ],
                }
            ]
        }
    )

    boundaries, problems = _validate_shadow_plan(
        engine.vault,
        args,
        scope=PurePosixPath("아폴로/2023"),
        handles=handles,
    )

    assert problems == []
    assert boundaries[0].moves[0].target == "아폴로/2023/새분류"


async def test_add_sibling_coalesces_same_parent_existing_routing(engine: Bismuth) -> None:
    await _four_documents(engine)
    parent = Path(engine.vault.root) / "아폴로/2023"
    (parent / "기존분류").mkdir()
    (parent / CHARTER_FILENAME).write_text(
        Charter(
            path=PurePosixPath("아폴로/2023"),
            title="2023",
            purpose="",
            split_basis="문서 주제",
            split_question="이 문서의 주제는 무엇인가?",
            split_at_documents=4,
        ).to_markdown(),
        encoding="utf-8",
    )
    handles = _document_handles(engine.vault)
    ids = list(handles)
    args = _SubmitPlanArgs.model_validate(
        {
            "boundaries": [
                {
                    "parent": "아폴로/2023",
                    "operation": "route_existing",
                    "moves": [{"document_ids": [ids[0]], "target": "기존분류"}],
                },
                {
                    "parent": "아폴로/2023",
                    "operation": "add_sibling",
                    "moves": [{"document_ids": ids[1:3], "target": "새분류"}],
                },
            ]
        }
    )

    boundaries, problems = _validate_shadow_plan(
        engine.vault,
        args,
        scope=PurePosixPath("아폴로/2023"),
        handles=handles,
    )

    assert problems == []
    assert len(boundaries) == 1
    assert boundaries[0].operation == "add_sibling"
    assert {PurePosixPath(move.target).name for move in boundaries[0].moves} == {
        "기존분류",
        "새분류",
    }


async def test_add_sibling_can_repair_a_focused_document_from_an_existing_child(
    engine: Bismuth,
) -> None:
    await _four_documents(engine)
    parent = Path(engine.vault.root) / "아폴로/2023"
    existing = parent / "기존분류"
    existing.mkdir()
    (parent / CHARTER_FILENAME).write_text(
        Charter(
            path=PurePosixPath("아폴로/2023"),
            title="2023",
            purpose="",
            split_basis="문서 주제",
            split_question="이 문서의 주제는 무엇인가?",
            split_at_documents=4,
        ).to_markdown(),
        encoding="utf-8",
    )
    for name in ("a.txt", "a.txt.md"):
        (parent / name).replace(existing / name)

    handles = _document_handles(engine.vault)
    by_name = {path.name: handle for handle, path in handles.items()}
    args = _SubmitPlanArgs.model_validate(
        {
            "boundaries": [
                {
                    "parent": "아폴로/2023",
                    "operation": "add_sibling",
                    "moves": [
                        {
                            "document_ids": [by_name["a.txt"], by_name["b.txt"]],
                            "target": "새분류",
                        },
                        {
                            "document_ids": [by_name["c.txt"]],
                            "target": "기존분류",
                        },
                    ],
                }
            ]
        }
    )

    boundaries, problems = _validate_shadow_plan(
        engine.vault,
        args,
        scope=PurePosixPath("아폴로/2023"),
        handles=handles,
    )

    assert problems == []
    assert {PurePosixPath(path).name for path in boundaries[0].moves[0].paths} == {
        "a.txt",
        "b.txt",
    }


async def test_family_unit_accepts_committed_target_member_as_anchor(engine: Bismuth) -> None:
    await _four_documents(engine)
    parent = Path(engine.vault.root) / "아폴로/2023"
    existing = parent / "기존분류"
    existing.mkdir()
    (parent / CHARTER_FILENAME).write_text(
        Charter(
            path=PurePosixPath("아폴로/2023"),
            title="2023",
            purpose="",
            split_basis="문서 주제",
            split_question="이 문서의 주제는 무엇인가?",
            split_at_documents=4,
        ).to_markdown(),
        encoding="utf-8",
    )
    for name in ("a.txt", "a.txt.md"):
        (parent / name).replace(existing / name)

    handles = _document_handles(engine.vault)
    by_name = {path.name: handle for handle, path in handles.items()}
    family_units = {"F001": (by_name["a.txt"], by_name["b.txt"])}
    args = _SubmitPlanArgs.model_validate(
        {
            "boundaries": [
                {
                    "parent": "아폴로/2023",
                    "operation": "route_existing",
                    "moves": [{"document_ids": ["F001"], "target": "기존분류"}],
                }
            ]
        }
    )

    boundaries, problems = _validate_shadow_plan(
        engine.vault,
        args,
        scope=PurePosixPath("아폴로/2023"),
        handles=handles,
        family_units=family_units,
        require_family_units=True,
    )

    assert problems == []
    assert boundaries[0].moves[0].paths == ["아폴로/2023/b.txt"]


async def test_new_scoped_shelves_accept_relative_class_names(engine: Bismuth) -> None:
    await _four_documents(engine)
    handles = _document_handles(engine.vault)
    ids = list(handles)
    args = _SubmitPlanArgs.model_validate(
        {
            "boundaries": [
                {
                    "parent": "아폴로/2023",
                    "operation": "create_boundary",
                    "axis": "문서 성격",
                    "axis_question": "이 문서는 어떤 성격인가?",
                    "moves": [
                        {"document_ids": ids[:2], "target": "계약"},
                        {"document_ids": ids[2:], "target": "보고"},
                    ],
                }
            ]
        }
    )

    boundaries, problems = _validate_shadow_plan(
        engine.vault,
        args,
        scope=PurePosixPath("아폴로/2023"),
        handles=handles,
    )

    assert problems == []
    assert [move.target for move in boundaries[0].moves] == [
        "아폴로/2023/계약",
        "아폴로/2023/보고",
    ]


async def test_repeated_reads_are_not_reported_as_a_successful_no_change(
    engine: Bismuth,
) -> None:
    await _four_documents(engine)
    model = FakeModel([says("", call("tree", {}, call_id=f"turn-{index}")) for index in range(24)])

    proposal = await _svc(engine, model).propose_reorg()

    assert proposal.moves == []
    assert "safety guard" in " ".join(proposal.problems)


async def test_arrival_window_exposes_only_focus_cards_with_short_handles(
    engine: Bismuth,
) -> None:
    await _four_documents(engine)
    document_ids = [document_id for document_id, _ in engine.catalog.iter_cards()]
    model = FakeModel(
        [
            says("", call("arrivals", {})),
            says("", call("inventory", {"path": "", "recursive": True})),
            says("탐색을 마쳤습니다."),
            says(
                "",
                call(
                    "finish_no_change",
                    {"reason": "두 도착 문서만으로는 기존 경계를 바꿀 근거가 없습니다."},
                ),
            ),
        ]
    )

    await _svc(engine, model).propose_reorg(focus_document_ids=document_ids[:2])

    arrival_output = next(
        message.content for message in model.calls[1][1] if message.role == "tool"
    )
    inventory_output = next(
        message.content for message in model.calls[2][1] if message.role == "tool"
    )
    assert arrival_output.count("ID=D") == 2
    assert inventory_output.count("ID=D") == 2
    assert all(document_id not in arrival_output for document_id in document_ids)


async def test_focused_inventory_marks_committed_documents_reference_only(
    engine: Bismuth,
) -> None:
    await _four_documents(engine)
    parent = Path(engine.vault.root) / "아폴로/2023"
    existing = parent / "기존분류"
    existing.mkdir()
    (parent / CHARTER_FILENAME).write_text(
        Charter(
            path=PurePosixPath("아폴로/2023"),
            title="2023",
            purpose="",
            split_basis="문서 주제",
            split_question="이 문서의 주제는 무엇인가?",
            split_at_documents=4,
        ).to_markdown(),
        encoding="utf-8",
    )
    for name in ("c.txt", "c.txt.md", "d.txt", "d.txt.md"):
        (parent / name).replace(existing / name)
    ids_by_filename = {
        engine.catalog.load_source(document_id).filename: document_id  # type: ignore[union-attr]
        for document_id, _ in engine.catalog.iter_cards()
    }
    focus = [ids_by_filename["a.txt"], ids_by_filename["b.txt"]]
    model = FakeModel(
        [
            says("", call("inventory", {"path": "아폴로/2023", "recursive": True})),
            says("탐색을 마쳤습니다."),
            says(
                "",
                call(
                    "finish_no_change",
                    {"reason": "현재 두 도착 문서만으로는 새 형제 경계를 만들 근거가 없습니다."},
                ),
            ),
        ]
    )

    await _svc(engine, model).propose_reorg(
        scope="아폴로/2023",
        focus_document_ids=focus,
    )

    inventory_output = next(
        message.content for message in model.calls[1][1] if message.role == "tool"
    )
    assert inventory_output.count("ID=D") == 2
    assert inventory_output.count("ID=R") == 2
    conclusion_prompt = model.calls[-1][1][0].content
    assert "every R handle in observations is reference-only" in conclusion_prompt


async def test_bounded_established_scope_hides_destructive_operations(
    engine: Bismuth,
) -> None:
    await _four_documents(engine)
    parent = Path(engine.vault.root) / "아폴로/2023"
    (parent / "기존분류").mkdir()
    (parent / CHARTER_FILENAME).write_text(
        Charter(
            path=PurePosixPath("아폴로/2023"),
            title="2023",
            purpose="",
            split_basis="문서 주제",
            split_question="이 문서의 주제는 무엇인가?",
            split_at_documents=4,
        ).to_markdown(),
        encoding="utf-8",
    )
    document_ids = [document_id for document_id, _ in engine.catalog.iter_cards()]
    model = FakeModel(
        [
            says("탐색을 마쳤습니다."),
            says(
                "",
                call(
                    "finish_no_change",
                    {"reason": "기존 경계를 변경할 충분한 증거가 없어 현재 구조를 유지합니다."},
                ),
            ),
        ]
    )

    await _svc(engine, model).propose_reorg(
        scope="아폴로/2023",
        focus_document_ids=document_ids,
    )

    submit = next(tool for tool in model.calls[-1][2] if tool.name == "submit_plan")
    schema_text = str(submit.parameters)
    assert "route_existing" in schema_text
    assert "rehome_existing" in schema_text
    assert "add_sibling" in schema_text
    assert "create_boundary" not in schema_text
    assert "replace_boundary" not in schema_text


async def test_arrivals_marks_grounded_family_members_with_their_exact_handles(
    engine: Bismuth,
) -> None:
    await _four_documents(engine)
    ids_by_filename = {
        engine.catalog.load_source(document_id).filename: document_id  # type: ignore[union-attr]
        for document_id, _ in engine.catalog.iter_cards()
    }
    document_ids = [ids_by_filename["a.txt"], ids_by_filename["b.txt"]]
    for document_id, filename, title in (
        (document_ids[0], "과학관설립운영법.txt", "과학관설립운영법"),
        (document_ids[1], "과학관설립운영법 시행령.txt", "과학관설립운영법 시행령"),
    ):
        card = engine.catalog.load_card(document_id)
        source = engine.catalog.load_source(document_id)
        assert card is not None and source is not None
        engine.catalog.save_card(
            document_id,
            card.model_copy(update={"title": title}),
            source=source.model_copy(update={"filename": filename}),
        )
    assert family_components(engine.catalog, document_ids) == [document_ids]
    model = FakeModel(
        [
            says("", call("arrivals", {})),
            says("탐색을 마쳤습니다."),
            says(
                "",
                call(
                    "finish_no_change",
                    {"reason": "표시된 family 전체를 이동할 충분한 경계 근거가 없습니다."},
                ),
            ),
        ]
    )

    await _svc(engine, model).propose_reorg(focus_document_ids=document_ids[:2])

    output = next(message.content for message in model.calls[1][1] if message.role == "tool")
    family_rows = [line for line in output.splitlines() if "FAMILY_UNIT=F001" in line]
    assert len(family_rows) == 2
    assert all("FAMILY_MEMBERS=D000001,D000002" in row for row in family_rows)
    assert all("ASSIGN_WITH=F001" in row for row in family_rows)


async def test_small_flat_shelf_is_not_immediately_subdivided(engine: Bismuth) -> None:
    await _four_documents(engine)
    document_ids = [document_id for document_id, _ in engine.catalog.iter_cards()]

    assert _svc(engine, FakeModel([])).next_affected_scope(document_ids) is None


async def test_leaf_arrival_reopens_its_established_boundary_parent(engine: Bismuth) -> None:
    await _four_documents(engine)
    parent = Path(engine.vault.root) / "아폴로/2023"
    child = parent / "기존분류"
    child.mkdir()
    (parent / "다른분류").mkdir()
    (parent / CHARTER_FILENAME).write_text(
        Charter(
            path=PurePosixPath("아폴로/2023"),
            title="2023",
            purpose="",
            split_basis="문서 주제",
            split_question="이 문서의 주제는 무엇인가?",
            split_at_documents=4,
        ).to_markdown(),
        encoding="utf-8",
    )
    for name in ("a.txt", "a.txt.md"):
        (parent / name).replace(child / name)
    document_id = next(
        document_id
        for document_id, card in engine.catalog.iter_cards()
        if engine.catalog.load_source(document_id).filename == "a.txt"  # type: ignore[union-attr]
    )

    scope = _svc(engine, FakeModel([])).next_affected_scope([document_id])

    assert scope == ("아폴로/2023", (document_id,))


def test_upload_places_each_document_without_enqueuing_a_maintenance_window(
    settings: Settings, llm: object
) -> None:
    chat = FakeModel(
        handler=lambda *_: says(
            "",
            call(
                "finish_placement",
                {"action": "place_existing", "folder_id": "F0002"},
            ),
        )
    )
    app = create_app(settings)
    organized = build(settings, llm=llm, chat_model=chat)  # type: ignore[arg-type]
    seed_folder(Path(organized.vault.root))
    app.state.engine = organized

    files = [
        ("files", (name, f"서로 다른 문서 {index}".encode(), "text/plain"))
        for index, name in enumerate(("a.txt", "b.txt", "c.txt", "d.txt"), start=1)
    ]
    with TestClient(app) as client:
        response = client.post("/api/documents", files=files)

        assert response.status_code == 200
        paths = [folder["path"] for folder in client.get("/api/tree").json()]
        maintenance = client.get("/api/maintenance").json()
        assert "아폴로/2023" in paths
        assert maintenance["status"] == "idle"
        assert maintenance["pending_documents"] == 0
        assert maintenance["deferred_documents"] == 0
        assert len(chat.calls) == 4


def test_manual_structure_retry_ui_and_api_are_removed(settings: Settings, llm: object) -> None:
    app = create_app(settings)
    app.state.engine = build(settings, llm=llm, chat_model=FakeModel([]))  # type: ignore[arg-type]

    with TestClient(app) as client:
        response = client.post("/api/maintenance/retry")
        html = client.get("/").text

    assert response.status_code == 404
    assert "구조 정리 계속" not in html
    assert 'id="btn-organize"' not in html
