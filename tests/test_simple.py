"""Simple-batch filing and tree shaping."""

from __future__ import annotations

from pathlib import PurePosixPath

from bismuth.container import Bismuth
from bismuth.prompts import shaping as shaping_prompts
from bismuth.prompts import simple as simple_prompts
from bismuth.services import simple as simple_service
from tests.conftest import ScriptedModel

NEAREST = "DOCUMENTS TO ROUTE"
SHAPING = "CURRENT FOLDERS"
PARENT_SCOPE = "PROPOSED CHILD:"
REVIEW = "LOOSE AT ROOT"
REFILE = "CURRENT FOLDER"
GROUPING = "FOLDERS AT"


class Answers:
    """A model that replies with whatever the test lined up for each question.

    Keyed by stage rather than by order: one batch now costs a nearest and a shaping, and a
    list of replies in order would have every test counting calls it does not care about.
    """

    def __init__(self, script: ScriptedModel) -> None:
        self.script = script
        self.nearest: list[str] = []
        self.shaping: list[str] = []
        self.scope: list[str] = []
        self.review: list[str] = []
        self.refile: list[str] = []
        self.grouping: list[str] = []
        self.asked: list[str] = []

    def __call__(self, prompt, schema):  # type: ignore[no-untyped-def]
        for mark, queued, quiet in (
            (NEAREST, self.nearest, ""),
            (SHAPING, self.shaping, ""),
            (PARENT_SCOPE, self.scope, "KEEP"),
            (REVIEW, self.review, "KEEP"),
            (REFILE, self.refile, ""),
            (GROUPING, self.grouping, "NONE"),
        ):
            if mark in prompt.user:
                self.asked.append(prompt.user)
                return queued.pop(0) if queued else quiet
        return self.script(prompt, schema)


async def _staged(engine: Bismuth, count: int) -> list:
    """Documents read and catalogued, sitting in the inbox, not yet filed."""
    out = []
    batch_token = engine.catalog.card_count()
    for index in range(count):
        rel = engine.ingest.stage(
            f"문서 {batch_token}-{index} 내용".encode(),
            f"doc{index}.txt",
        )
        out.append(await engine.ingest.prepare(rel))
    return out


def _batch(prepared: list) -> list:
    return [(one.rel, one.card, one) for one in prepared]


def _handle(answers: Answers, name: str) -> str:
    """The ``F`` number a folder was listed under in the last question asked."""
    for line in answers.asked[-1].splitlines():
        if f"] {name}/" in line:
            return line.split("]")[0].strip().lstrip("[")
    raise AssertionError(f"{name} was not listed: {answers.asked[-1]}")


class TestFilingAHandfulAtOnce:
    async def test_two_calls_file_the_whole_batch(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        answers = Answers(script)
        llm.set_handler(answers)
        prepared = await _staged(engine, 3)
        answers.nearest = ["D1: NONE\nD2: NONE\nD3: NONE"]
        answers.shaping = ["CREATE: 문학 | D1, D2\nROOT: D3\nSIGN: 문학 | 소설과 시를 모아둔다"]

        await engine.simple.file(_batch(prepared))

        assert len(answers.asked) == 2, "one pair for the batch, not one pair per document"
        assert (engine.vault.root / "문학/doc0.txt").is_file()
        assert (engine.vault.root / "문학/doc1.txt").is_file()
        assert (engine.vault.root / "doc2.txt").is_file(), "the root means the pile, honestly"
        assert (engine.vault.root / "문학/_folder.md").is_file()
        assert engine.catalog.card_count() == 3

    async def test_a_named_folder_carries_the_sign_it_was_given(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        answers = Answers(script)
        llm.set_handler(answers)
        prepared = await _staged(engine, 2)
        answers.nearest = ["D1: NONE\nD2: NONE"]
        answers.shaping = ["CREATE: 문학 | D1, D2\nSIGN: 문학 | 소설과 시를 모아둔다"]

        await engine.simple.file(_batch(prepared))

        note = engine.charters.load(PurePosixPath("문학"))
        assert note is not None
        assert note.purpose == "소설과 시를 모아둔다"

    async def test_one_document_may_establish_a_reusable_folder(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        answers = Answers(script)
        llm.set_handler(answers)
        prepared = await _staged(engine, 1)
        answers.nearest = ["D1: NONE"]
        answers.shaping = ["CREATE: 문학 | D1\nSIGN: 문학 | 소설과 시에 관한 문서"]

        await engine.simple.file(_batch(prepared))

        assert (engine.vault.root / "문학/doc0.txt").is_file()
        note = engine.charters.load(PurePosixPath("문학"))
        assert note is not None and note.purpose == "소설과 시에 관한 문서"

    async def test_one_document_may_create_a_reusable_child(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        answers = Answers(script)
        llm.set_handler(answers)
        first = await _staged(engine, 1)
        answers.nearest = ["D1: NONE"]
        answers.shaping = ["CREATE: 문화 | D1\nSIGN: 문화 | 문화 활동과 작품"]
        await engine.simple.file(_batch(first))

        arriving = await _staged(engine, 1)
        answers.nearest = ["D1: 문화"]
        answers.shaping = ["BELOW: F1 | 문학 | D1\nSIGN: 문학 | 소설과 시에 관한 문서"]
        await engine.simple.file(_batch(arriving))

        assert (engine.vault.root / "문화/문학/doc0.txt").is_file()

    async def test_a_second_batch_is_shown_what_the_first_built(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        answers = Answers(script)
        llm.set_handler(answers)
        first = await _staged(engine, 2)
        answers.nearest = ["D1: NONE\nD2: NONE"]
        answers.shaping = ["CREATE: 문학 | D1, D2\nSIGN: 문학 | 소설과 시"]
        await engine.simple.file(_batch(first))
        second = await _staged(engine, 1)
        answers.nearest = ["D1: 문학"]
        answers.shaping = ["INSIDE: F1 | D1"]

        await engine.simple.file(_batch(second))

        assert "문학/  (2 documents) — 소설과 시" in answers.asked[-2], "the tree, as it now stands"
        landed = list((engine.vault.root / "문학").glob("*.txt"))
        assert len(landed) == 3, "the third document joined the two already there"

    async def test_a_document_may_be_turned_away_from_where_it_was_sent(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """The first question picks the nearest place, not the right one; the second may refuse."""
        answers = Answers(script)
        llm.set_handler(answers)
        first = await _staged(engine, 2)
        answers.nearest = ["D1: NONE\nD2: NONE"]
        answers.shaping = ["CREATE: 문학 | D1, D2\nSIGN: 문학 | 소설과 시"]
        await engine.simple.file(_batch(first))
        stray = await _staged(engine, 1)
        answers.nearest = ["D1: 문학"]
        answers.shaping = ["ROOT: D1"]

        await engine.simple.file(_batch(stray))

        assert list(engine.vault.root.glob("*.txt")), "turned away, and left at the root"
        assert len(list((engine.vault.root / "문학").glob("*.txt"))) == 2, "not taken in"

    async def test_an_overfull_folder_refuses_inside(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "DIRECT_LIMIT", 2)
        answers = Answers(script)
        llm.set_handler(answers)
        first = await _staged(engine, 2)
        answers.nearest = ["D1: NONE\nD2: NONE"]
        answers.shaping = ["CREATE: 문학 | D1, D2"]
        await engine.simple.file(_batch(first))

        arriving = await _staged(engine, 1)
        answers.nearest = ["D1: 문학"]
        answers.shaping = ["INSIDE: F1 | D1"]
        await engine.simple.file(_batch(arriving))

        assert len(list((engine.vault.root / "문학").glob("*.txt"))) == 2
        assert len(list(engine.vault.root.glob("*.txt"))) == 1

    async def test_silence_does_not_fall_back_into_an_overfull_folder(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "DIRECT_LIMIT", 2)
        answers = Answers(script)
        llm.set_handler(answers)
        first = await _staged(engine, 2)
        answers.nearest = ["D1: NONE\nD2: NONE"]
        answers.shaping = ["CREATE: 문학 | D1, D2"]
        await engine.simple.file(_batch(first))

        arriving = await _staged(engine, 1)
        answers.nearest = ["D1: 문학"]
        answers.shaping = [""]
        await engine.simple.file(_batch(arriving))

        assert len(list((engine.vault.root / "문학").glob("*.txt"))) == 2
        assert len(list(engine.vault.root.glob("*.txt"))) == 1


class TestTheQuestionSaysWhatItIsShowing:
    """Compressed, a line stops explaining itself, and a folder that has been divided reads
    as an empty one unless the line says what is below it."""

    def test_a_divided_folder_does_not_read_as_an_empty_one(self) -> None:
        folder = simple_prompts.Folder(
            path=PurePosixPath("금융"), note="금융 규제", documents=0, held=70, children=6
        )
        line = simple_prompts._size(folder)

        assert "70 total" in line and "0 direct" in line and "6 children" in line

    def test_an_undivided_folder_says_only_its_own_count(self) -> None:
        folder = simple_prompts.Folder(path=PurePosixPath("금융"), note="", documents=4)

        assert simple_prompts._size(folder) == "(4 documents)"

    def test_the_shaping_question_says_how_big_the_place_really_is(self) -> None:
        place = shaping_prompts.Place(
            folder=PurePosixPath("금융"),
            note="금융 규제",
            holding=["은행법 | 법률 | 은행"] * 12,
            held=32,
            children=[],
            arriving=[("D1", "보험업법 | 법률 | 보험")],
        )
        prompt = shaping_prompts.build_shaping(folders=[], places=[place], homeless=[])

        assert "32 direct document(s)" in prompt.user
        assert "20 more not shown" in prompt.user, "a list cut short must say it was cut"

    def test_prompts_keep_the_hard_size_and_audit_rules(self) -> None:
        shaping = shaping_prompts.build_shaping(folders=[], places=[], homeless=[]).system
        review = simple_prompts.build_review(folders=[], total=0, loose=0).system

        assert "25 or more direct documents" in shaping
        assert "do not use `INSIDE`" in shaping
        assert all(f"CHECK{number}:" in review for number in range(1, 10))

    def test_shaping_allows_one_current_document_but_forbids_a_one_document_scope(self) -> None:
        shaping = shaping_prompts.build_shaping(
            folders=[], places=[], homeless=[("D1", "작품 제목 | 보고서 | 주제")]
        ).system

        assert "single arriving document is enough" in shaping
        assert "currently contains one document" in shaping
        assert "scope can contain only that document" in shaping
        assert "Do not copy or lightly shorten a document title" in shaping
        scope = shaping_prompts.build_parent_scope(
            parent=simple_prompts.Folder(
                path=PurePosixPath("금융채권"), note="채권과 채무자 보호", documents=2
            ),
            child="보험업 규제",
            documents=["보험업법 | 법률 | 보험회사"],
        ).system
        assert "strict" in scope and "containment check" in scope
        assert "Sharing a broad domain is insufficient" in scope


class TestLookingAtTheWholeTree:
    async def test_it_waits_until_the_collection_is_worth_judging(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        answers = Answers(script)
        llm.set_handler(answers)
        prepared = await _staged(engine, 3)
        answers.nearest = ["D1: NONE\nD2: NONE\nD3: NONE"]
        answers.shaping = ["CREATE: 문학 | D1, D2, D3"]
        await engine.simple.file(_batch(prepared))

        assert not engine.simple.due(), "three documents is not a tree to judge"

    async def test_the_end_of_an_upload_is_looked_at_even_without_a_doubling(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "FIRST_REVIEW", 2)
        answers = Answers(script)
        llm.set_handler(answers)
        prepared = await _staged(engine, 2)
        answers.nearest = ["D1: NONE\nD2: NONE"]
        answers.shaping = ["CREATE: 문학 | D1, D2"]
        await engine.simple.file(_batch(prepared))
        await engine.simple.review()

        assert not engine.simple.due(), "just judged, and nothing has arrived since"
        assert engine.simple.due(settling=True) is False, "nor has anything been left unjudged"

        more = await _staged(engine, 1)
        answers.nearest = ["D1: 문학"]
        answers.shaping = ["INSIDE: F1 | D1"]
        await engine.simple.file(_batch(more))

        assert not engine.simple.due(), "three is not twice two"
        assert engine.simple.due(settling=True), "but the third document has never been judged"

    async def test_keep_moves_nothing(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "FIRST_REVIEW", 2)
        answers = Answers(script)
        llm.set_handler(answers)
        prepared = await _staged(engine, 2)
        answers.nearest = ["D1: NONE\nD2: NONE"]
        answers.shaping = ["CREATE: 문학 | D1, D2"]
        await engine.simple.file(_batch(prepared))
        answers.review = ["검사1: NONE\n검사5: 1개\n\nKEEP"]

        assert engine.simple.due()
        assert not await engine.simple.review()
        assert (engine.vault.root / "문학/doc0.txt").is_file()

    async def test_a_mature_review_drains_even_one_root_document(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "FIRST_REVIEW", 2)
        answers = Answers(script)
        llm.set_handler(answers)
        prepared = await _staged(engine, 2)
        answers.nearest = ["D1: NONE\nD2: NONE"]
        answers.shaping = ["CREATE: 문학 | D1\nROOT: D2"]
        await engine.simple.file(_batch(prepared))
        answers.review = ["KEEP"]
        answers.refile = ["D1: 문학"]

        assert await engine.simple.review()
        assert not list(engine.vault.root.glob("*.txt"))

    async def test_a_move_relocates_the_folder_whole(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "FIRST_REVIEW", 2)
        answers = Answers(script)
        llm.set_handler(answers)
        prepared = await _staged(engine, 2)
        answers.nearest = ["D1: NONE\nD2: NONE"]
        answers.shaping = ["CREATE: 문학 | D1, D2\nSIGN: 문학 | 소설과 시"]
        await engine.simple.file(_batch(prepared))
        answers.review = ["MOVE: 문학 | 인문/문학\nSIGN: 인문 | 사람이 쓴 것"]

        assert await engine.simple.review()

        assert (engine.vault.root / "인문/문학/doc0.txt").is_file()
        assert (engine.vault.root / "인문/문학/doc1.txt").is_file()
        assert not (engine.vault.root / "문학").exists()
        note = engine.charters.load(PurePosixPath("인문"))
        assert note is not None and note.purpose == "사람이 쓴 것"

    async def test_a_folder_cannot_be_moved_inside_itself(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "FIRST_REVIEW", 2)
        answers = Answers(script)
        llm.set_handler(answers)
        prepared = await _staged(engine, 2)
        answers.nearest = ["D1: NONE\nD2: NONE"]
        answers.shaping = ["CREATE: 문학 | D1, D2"]
        await engine.simple.file(_batch(prepared))
        answers.review = ["MOVE: 문학 | 문학/하위"]

        assert not await engine.simple.review()
        assert (engine.vault.root / "문학/doc0.txt").is_file()


class TestGroupingALevelThatWentWide:
    """Folders only. Shown documents as well, the reply answered with document numbers
    where folder numbers were wanted, and nothing was ever grouped."""

    async def _spread(self, engine: Bismuth, answers: Answers, names: list[str]) -> None:
        prepared = await _staged(engine, len(names) * 2)
        answers.nearest = ["\n".join(f"D{i}: NONE" for i in range(1, len(names) * 2 + 1))]
        answers.shaping = [
            "\n".join(
                f"CREATE: {name} | D{i * 2 + 1}, D{i * 2 + 2}" for i, name in enumerate(names)
            )
        ]
        await engine.simple.file(_batch(prepared))

    async def test_a_narrow_level_is_left_alone(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        answers = Answers(script)
        llm.set_handler(answers)
        await self._spread(engine, answers, ["가", "나"])
        before = len(answers.asked)

        assert not await engine.simple.regroup()
        assert len(answers.asked) == before, "two folders are not a level worth asking about"

    async def test_a_wide_level_is_grouped(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "WIDE", 2)
        answers = Answers(script)
        llm.set_handler(answers)
        await self._spread(engine, answers, ["가", "나", "다"])
        answers.grouping = ["GROUP: 묶음 | F1, F2", "NONE"]

        assert await engine.simple.regroup()

        assert (engine.vault.root / "묶음/가").is_dir()
        assert (engine.vault.root / "묶음/나").is_dir()
        assert (engine.vault.root / "다").is_dir(), "what was left out stays where it was"

    async def test_a_parent_that_would_hold_one_folder_is_refused(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "WIDE", 2)
        answers = Answers(script)
        llm.set_handler(answers)
        await self._spread(engine, answers, ["가", "나", "다"])
        answers.grouping = ["GROUP: 묶음 | F1"]

        assert not await engine.simple.regroup()
        assert not (engine.vault.root / "묶음").exists()

    async def test_one_stray_may_move_under_a_folder_that_already_stands(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """The two-folder rule is about inventing a parent, not about using one."""
        monkeypatch.setattr(simple_service, "WIDE", 2)
        answers = Answers(script)
        llm.set_handler(answers)
        await self._spread(engine, answers, ["가", "나", "다"])
        answers.grouping = ["GROUP: F1 | F3", "NONE"]

        assert await engine.simple.regroup()
        assert (engine.vault.root / "가/다").is_dir()

    async def test_a_folder_cannot_be_put_under_itself(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "WIDE", 2)
        answers = Answers(script)
        llm.set_handler(answers)
        await self._spread(engine, answers, ["가", "나", "다"])
        answers.grouping = ["GROUP: 가 | F1, F2"]

        assert await engine.simple.regroup()
        assert (engine.vault.root / "가/나").is_dir(), "the one that was not itself moved"
        assert (engine.vault.root / "가").is_dir() and not (engine.vault.root / "가/가").exists()


class TestWhatTheReplyMaySay:
    def test_a_document_with_no_line_stays_at_the_root(self) -> None:
        placed, _ = simple_prompts.parse_filing("D1: 문학")

        assert placed == {"D1": "문학"}

    def test_a_line_nobody_asked_for_is_dropped(self) -> None:
        placed, signs = simple_prompts.parse_filing("여기 있습니다:\nD1: 문학\n감사합니다.")

        assert placed == {"D1": "문학"}
        assert not signs

    def test_keep_and_an_empty_reply_mean_the_same(self) -> None:
        assert simple_prompts.parse_review("KEEP").keep
        assert simple_prompts.parse_review("").keep
        assert simple_prompts.parse_review("이 트리는 괜찮습니다.").keep

    def test_a_refile_line_names_a_folder_to_redraw(self) -> None:
        asked = simple_prompts.parse_review("REFILE: 금융\nREFILE: /금융/\nMOVE: 가 | 나")

        assert asked.refile == (PurePosixPath("금융"),), "the same folder twice is once"
        assert not asked.keep, "asking for a refile is not keeping the tree"

    def test_a_refile_may_rename_the_shelf_it_divides(self) -> None:
        placed, signs = simple_prompts.parse_filing("D1: 은행\nRENAME: 금융업 규제")

        assert placed == {"D1": "은행"}
        assert signs[simple_prompts.RENAME] == "금융업 규제"

    def test_folders_are_named_by_handle_and_documents_by_handle(self) -> None:
        folders = [
            simple_prompts.Folder(path=PurePosixPath(name), note="", documents=2)
            for name in ("가", "나")
        ]
        shaped = shaping_prompts.parse_shaping(
            "INSIDE: F2 | D1, D3\nBELOW: F1 | 아래 | D2\nCREATE: 새것 | D4, D5\nROOT: D9",
            folders,
        )

        assert shaped.inside == {"나": ["D1", "D3"]}
        assert shaped.below == {"가/아래": ["D2"]}
        assert shaped.made == {"새것": ["D4", "D5"]}
        assert shaped.loose == ["D9"]

    async def test_a_child_outside_its_parent_becomes_a_sibling(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        answers = Answers(script)
        llm.set_handler(answers)
        first = await _staged(engine, 1)
        answers.nearest = ["D1: NONE"]
        answers.shaping = ["CREATE: 채무자 보호 | D1"]
        await engine.simple.file(_batch(first))

        arriving = await _staged(engine, 1)
        answers.nearest = ["D1: 채무자 보호"]
        answers.shaping = ["BELOW: F1 | 보험업 규제 | D1"]
        answers.scope = ["SIBLING"]
        await engine.simple.file(_batch(arriving))

        assert not (engine.vault.root / "채무자 보호/보험업 규제").exists()
        assert (engine.vault.root / "보험업 규제/doc0.txt").is_file()

    def test_parent_scope_can_promote_or_make_a_sibling(self) -> None:
        assert shaping_prompts.parse_parent_scope("KEEP") == ("keep", "", "")
        assert shaping_prompts.parse_parent_scope("SIBLING") == ("sibling", "", "")
        assert shaping_prompts.parse_parent_scope(
            "PROMOTE: 금융 규제 | 금융산업의 제도와 감독"
        ) == ("promote", "금융 규제", "금융산업의 제도와 감독")

    def test_a_document_named_where_a_folder_belongs_is_dropped(self) -> None:
        """Grouping accepts folder handles only."""
        folders = [simple_prompts.Folder(path=PurePosixPath("가"), note="", documents=2)]
        groups, _ = shaping_prompts.parse_grouping(
            "GROUP: 새 부모 | 어떤 문서 제목, 또 다른 제목", folders
        )

        assert groups == []


class TestRedrawingOneFolder:
    """A folder that outgrew its own name is divided from the inside, and only inside."""

    async def _piled(self, engine: Bismuth, answers: Answers, count: int) -> None:
        prepared = await _staged(engine, count)
        answers.nearest = ["\n".join(f"D{i}: NONE" for i in range(1, count + 1))]
        answers.shaping = ["CREATE: 금융 | " + ", ".join(f"D{i}" for i in range(1, count + 1))]
        await engine.simple.file(_batch(prepared))

    async def test_a_mature_root_must_be_drained(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "FIRST_REVIEW", 2)
        answers = Answers(script)
        llm.set_handler(answers)
        first = await _staged(engine, 1)
        answers.nearest = ["D1: NONE"]
        answers.shaping = ["CREATE: 금융 | D1"]
        await engine.simple.file(_batch(first))

        loose = await _staged(engine, 2)
        answers.nearest = ["D1: NONE\nD2: NONE"]
        answers.shaping = ["ROOT: D1, D2"]
        await engine.simple.file(_batch(loose))
        answers.refile = ["D1: 금융\nD2: 보험 규제\nSIGN: 보험 규제 | 보험산업의 제도와 감독"]

        assert engine.simple.due(), "a mature tree cannot leave even one root document"
        assert await engine.simple.refile(PurePosixPath())
        assert not list(engine.vault.root.glob("*.txt"))
        assert (engine.vault.root / "금융/doc0 (2).txt").is_file()
        assert (engine.vault.root / "보험 규제/doc1.txt").is_file()

    async def test_the_pile_becomes_sub_folders_of_the_same_folder(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        answers = Answers(script)
        llm.set_handler(answers)
        await self._piled(engine, answers, 4)
        answers.refile = ["D1: 은행\nD2: 은행\nD3: 보험\nD4: 보험\nSIGN: 은행 | 은행이 하는 일"]

        assert await engine.simple.refile(PurePosixPath("금융"))

        assert (engine.vault.root / "금융/은행/doc0.txt").is_file()
        assert (engine.vault.root / "금융/보험/doc3.txt").is_file()
        assert (engine.vault.root / "금융/은행/doc0.txt.md").is_file(), "the sidecar travels too"
        note = engine.charters.load(PurePosixPath("금융/은행"))
        assert note is not None and note.purpose == "은행이 하는 일"

    async def test_nothing_leaves_the_folder_being_redrawn(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        answers = Answers(script)
        llm.set_handler(answers)
        await self._piled(engine, answers, 4)
        answers.refile = ["D1: 과학\nD2: 과학\nD3: /과학/물리\nD4: 금융/과학"]

        assert await engine.simple.refile(PurePosixPath("금융"))

        assert not (engine.vault.root / "과학").exists(), "an answer is a name below, not a path"
        assert (engine.vault.root / "금융/과학/doc0.txt").is_file()
        assert (engine.vault.root / "금융/과학/doc3.txt").is_file(), "금융/과학 is 과학, once"

    async def test_a_document_that_fits_nowhere_stays(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        answers = Answers(script)
        llm.set_handler(answers)
        await self._piled(engine, answers, 4)
        answers.refile = ["D1: 은행\nD2: 은행\nD3: 은행\nD4: STAY"]

        assert await engine.simple.refile(PurePosixPath("금융"))

        assert (engine.vault.root / "금융/doc3.txt").is_file()
        assert (engine.vault.root / "금융/은행/doc0.txt").is_file()

    async def test_one_sub_folder_taking_everything_is_refused(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        answers = Answers(script)
        llm.set_handler(answers)
        await self._piled(engine, answers, 4)
        answers.refile = ["D1: 금융법\nD2: 금융법\nD3: 금융법\nD4: 금융법"]

        assert not await engine.simple.refile(PurePosixPath("금융"))

        assert (engine.vault.root / "금융/doc0.txt").is_file()
        assert not (engine.vault.root / "금융/금융법").exists()

    async def test_a_small_folder_is_left_alone(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        answers = Answers(script)
        llm.set_handler(answers)
        await self._piled(engine, answers, 2)
        before = len(answers.asked)

        assert not await engine.simple.refile(PurePosixPath("금융"))
        assert len(answers.asked) == before, "two documents are not a pile"

    async def test_the_whole_redraw_is_one_journal_entry(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "BATCH", 2)
        answers = Answers(script)
        llm.set_handler(answers)
        await self._piled(engine, answers, 4)
        before = len(list(engine.journal.iter_entries()))
        answers.refile = ["D1: 은행\nD2: 은행", "D1: 보험\nD2: 보험"]

        assert await engine.simple.refile(PurePosixPath("금융"))

        entries = list(engine.journal.iter_entries())
        assert len(entries) - before == 1, "two calls, one entry: half a refile is the worst one"

    async def test_a_later_batch_sees_what_an_earlier_one_drew(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "BATCH", 2)
        answers = Answers(script)
        llm.set_handler(answers)
        await self._piled(engine, answers, 4)
        answers.refile = ["D1: 은행\nD2: 은행\nSIGN: 은행 | 은행이 하는 일", "D1: 보험\nD2: 보험"]

        await engine.simple.refile(PurePosixPath("금융"))

        assert "은행/  (2 documents) — 은행이 하는 일" in answers.asked[-1]

    async def test_a_later_batch_lists_an_existing_child_only_once(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "BATCH", 2)
        answers = Answers(script)
        llm.set_handler(answers)
        await self._piled(engine, answers, 4)
        answers.refile = ["D1: 은행\nD2: 은행", "D1: 은행\nD2: 보험"]

        await engine.simple.refile(PurePosixPath("금융"))

        child_lines = [line for line in answers.asked[-1].splitlines() if "은행/" in line]
        assert child_lines == ["  은행/  (2 documents)"]

    async def test_refile_does_not_overfill_an_existing_child(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        answers = Answers(script)
        llm.set_handler(answers)
        await self._piled(engine, answers, 4)
        answers.refile = ["D1: 은행\nD2: 은행\nD3: 보험\nD4: 보험"]
        assert await engine.simple.refile(PurePosixPath("금융"))

        arriving = await _staged(engine, 4)
        answers.nearest = ["\n".join(f"D{i}: 금융" for i in range(1, 5))]
        answers.shaping = ["INSIDE: F1 | D1, D2, D3, D4"]
        await engine.simple.file(_batch(arriving))

        monkeypatch.setattr(simple_service, "DIRECT_LIMIT", 3)
        answers.refile = ["D1: 은행\nD2: 은행\nD3: 보험\nD4: 보험"]
        assert await engine.simple.refile(PurePosixPath("금융"))

        assert len(list((engine.vault.root / "금융/은행").glob("*.txt"))) == 3
        assert len(list((engine.vault.root / "금융/보험").glob("*.txt"))) == 3
        assert len(list((engine.vault.root / "금융").glob("*.txt"))) == 2

    async def test_a_refile_may_rename_the_shelf_it_divides(
        self, engine: Bismuth, script: ScriptedModel, llm
    ) -> None:  # type: ignore[no-untyped-def]
        """The name was written when the folder held three documents and forty arrived after."""
        answers = Answers(script)
        llm.set_handler(answers)
        await self._piled(engine, answers, 4)
        answers.refile = ["D1: 은행\nD2: 은행\nD3: 보험\nD4: 보험\nRENAME: 금융업 규제"]

        assert await engine.simple.refile(PurePosixPath("금융"))

        assert (engine.vault.root / "금융업 규제/은행/doc0.txt").is_file()
        assert not (engine.vault.root / "금융").exists()

    async def test_a_review_may_ask_for_one_folder_to_be_redrawn(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "FIRST_REVIEW", 2)
        answers = Answers(script)
        llm.set_handler(answers)
        await self._piled(engine, answers, 4)
        answers.review = ["REFILE: 금융"]
        answers.refile = ["D1: 은행\nD2: 은행\nD3: 보험\nD4: 보험"]

        assert await engine.simple.review()

        assert (engine.vault.root / "금융/은행/doc0.txt").is_file()
        assert (engine.vault.root / "금융/보험/doc2.txt").is_file()

    async def test_review_refiles_an_overfull_folder_even_when_the_model_says_keep(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "FIRST_REVIEW", 2)
        monkeypatch.setattr(simple_service, "DIRECT_LIMIT", 3)
        answers = Answers(script)
        llm.set_handler(answers)
        await self._piled(engine, answers, 4)
        answers.review = ["KEEP"]
        answers.refile = ["D1: 은행\nD2: 은행\nD3: 보험\nD4: 보험"]

        assert await engine.simple.review()
        assert (engine.vault.root / "금융/은행/doc0.txt").is_file()
        assert (engine.vault.root / "금융/보험/doc2.txt").is_file()

    async def test_a_folder_is_redrawn_where_the_moves_left_it(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "FIRST_REVIEW", 2)
        answers = Answers(script)
        llm.set_handler(answers)
        await self._piled(engine, answers, 4)
        answers.review = ["MOVE: 금융 | 경제/금융\nREFILE: 금융"]
        answers.refile = ["D1: 은행\nD2: 은행\nD3: 보험\nD4: 보험"]

        assert await engine.simple.review()

        assert (engine.vault.root / "경제/금융/은행/doc0.txt").is_file()
        assert "CURRENT FOLDER: 경제/금융/" in answers.asked[-1]


class TestWhatAMoveMeans:
    """``MOVE: A | B`` says A becomes B. Asked to gather folders under a parent, the reply
    names the parent alone -- and read literally that renames one folder and then pours the
    next one into it, which turned twelve folders into a pile of sixty-two documents."""

    async def _three(self, engine: Bismuth, answers: Answers) -> None:
        prepared = await _staged(engine, 6)
        answers.nearest = ["\n".join(f"D{i}: NONE" for i in range(1, 7))]
        answers.shaping = ["CREATE: 가 | D1, D2\nCREATE: 나 | D3, D4\nCREATE: 다 | D5, D6"]
        await engine.simple.file(_batch(prepared))

    async def test_two_moves_onto_one_name_mean_that_name_is_a_parent(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "FIRST_REVIEW", 2)
        answers = Answers(script)
        llm.set_handler(answers)
        await self._three(engine, answers)
        answers.review = ["MOVE: 가 | 묶음\nMOVE: 나 | 묶음"]

        assert await engine.simple.review()

        assert (engine.vault.root / "묶음/가").is_dir(), "under it, not renamed onto it"
        assert (engine.vault.root / "묶음/나").is_dir()
        assert not list((engine.vault.root / "묶음").glob("*.txt")), "not poured into a pile"

    async def test_one_move_onto_a_folder_that_stands_goes_under_it(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "FIRST_REVIEW", 2)
        answers = Answers(script)
        llm.set_handler(answers)
        await self._three(engine, answers)
        answers.review = ["MOVE: 가 | 다"]

        assert await engine.simple.review()

        assert (engine.vault.root / "다/가").is_dir()
        assert len(list((engine.vault.root / "다").glob("*.txt"))) == 2, "what 다 held stays"

    async def test_one_move_onto_a_name_nobody_uses_is_a_rename(
        self, engine: Bismuth, script: ScriptedModel, llm, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(simple_service, "FIRST_REVIEW", 2)
        answers = Answers(script)
        llm.set_handler(answers)
        await self._three(engine, answers)
        answers.review = ["MOVE: 가 | 새이름"]

        assert await engine.simple.review()

        assert (engine.vault.root / "새이름").is_dir()
        assert not (engine.vault.root / "가").exists()
        assert not (engine.vault.root / "새이름/가").exists(), "a rename, not a nesting"
