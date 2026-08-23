"""Shared fixtures: a temp vault and engine backed by a scripted model."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from bismuth import logging_setup
from bismuth.adapters.llm.fake import FakeLLM
from bismuth.api.app import create_app
from bismuth.config import Settings
from bismuth.container import Bismuth, build
from bismuth.domain.charter import CHARTER_FILENAME, Charter
from bismuth.domain.document import Entity, EntityKind
from bismuth.ports.llm import Prompt
from bismuth.prompts import cards as card_prompts
from bismuth.prompts import charters as charter_prompts
from bismuth.prompts import placement as placement_prompts
from bismuth.prompts import redesign as redesign_prompts
from bismuth.prompts import subdivision as subdivision_prompts


@pytest.fixture(autouse=True)
def _logs_go_somewhere_disposable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite out of the repository's ./logs.

    LOG_DIR is relative, and anything that builds an app configures logging on startup --
    so running the tests truncated whatever run was being investigated at the time. The
    logs are the evidence; a test suite must not be able to destroy it.
    """
    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path / "logs")


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture(autouse=True)
def _config_stays_out_of_this(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No test reads or writes the config of whoever is running them.

    ``Settings`` binds its ``json_file`` when the class is defined, so the path has to
    be rebound as well as the module globals -- patching only the globals leaves every
    ``Settings()`` still reading ~/.bismuth/config.json. Until this existed a suite run
    inherited whatever model and key the machine was set up with, and an API test that
    saved a configuration wrote over them.
    """
    home = tmp_path_factory.mktemp("bismuth-home")
    monkeypatch.setattr("bismuth.config.CONFIG_DIR", home)
    monkeypatch.setattr("bismuth.config.CONFIG_FILE", home / "config.json")
    monkeypatch.setitem(Settings.model_config, "json_file", home / "config.json")


@pytest.fixture
def settings(vault_path: Path) -> Settings:
    return Settings(vault_path=vault_path)


def placement_to(folder: str | None, *, confidence: float | None = None):
    """Return a scripted direct-child chooser that walks to ``folder``."""
    del confidence  # compatibility with older test scripts; production has no such field
    target = PurePosixPath(folder or "")

    def decide(prompt: Prompt, schema: type[BaseModel] | None) -> str:
        if folder is None:
            return "UNREADABLE"
        current_line = next(
            (line for line in prompt.user.splitlines() if line.startswith("CURRENT FOLDER:")),
            None,
        )
        if current_line is None:
            # Not a placement descent. Bare next() here raised StopIteration inside a
            # coroutine, which surfaces as an unrelated RuntimeError.
            return "STAY"
        current_raw = current_line.partition(":")[2].strip()
        current = PurePosixPath() if current_raw == "(root)" else PurePosixPath(current_raw)
        if target.parts[: len(current.parts)] != current.parts or len(target.parts) <= len(
            current.parts
        ):
            return "STAY"
        wanted = target.parts[len(current.parts)]
        for line in prompt.user.splitlines():
            stripped = line.strip()
            if not stripped.startswith("[F"):
                continue
            folder_id, _, shown = stripped.partition("]")
            name = shown.strip().split(" — ", 1)[0]
            if name == wanted:
                return folder_id.removeprefix("[")
        return "STAY"

    return decide


def _shown_document(prompt: Prompt) -> str | None:
    """The one request-local document handle a closed-choice prompt is asking about."""
    for line in prompt.user.splitlines():
        stripped = line.strip()
        if stripped.startswith("[D"):
            return stripped.partition("]")[0].removeprefix("[")
    return None


def _scripted_emergence(model: ScriptedModel) -> subdivision_prompts.Emerging:
    """The Emerging a test scripted, which the chain's four steps are answered from."""
    scripted = model.responses[subdivision_prompts.Emerging]
    return scripted() if callable(scripted) else scripted


def _subset_shown(prompt: Prompt) -> list[str]:
    """Every handle in the prompt but the last -- a group has to leave something behind.

    Production refuses a group that takes the whole folder, so a fake that returned all
    of them would exercise the guard rather than the division the test is about.
    """
    shown = [
        line.strip()[1:6] for line in prompt.user.splitlines() if line.strip().startswith("[D")
    ]
    return shown[:-1] if len(shown) > 2 else shown


def _as_lines(card: BaseModel) -> str:
    """Render a scripted ``CardDraft``/``CardUpdate`` in the line format the reader now asks for.

    Tests script the card as an object because that is what they assert about. The
    contract on the wire changed, not what a test means, so the object is written out as
    the model would have written it and parsed back by the same code production uses.
    """
    lines: list[str] = []
    for tag, field in (("TITLE", "title"), ("DOCTYPE", "doc_type"), ("LANGUAGE", "language")):
        if value := (getattr(card, field, "") or "").strip():
            lines.append(f"{tag}: {value}")
    lines.append(f"SUMMARY: {getattr(card, 'summary', '')}")
    for tag, field in (
        ("TOPIC", "topics"),
        ("KEYWORD", "keywords"),
        ("QUESTION", "answers_questions"),
    ):
        for item in getattr(card, field, ()) or ():
            lines.append(f"{tag}: {item}")
    for tag, field in (
        ("TOPIC", "new_topics"),
        ("KEYWORD", "new_keywords"),
        ("QUESTION", "new_questions"),
    ):
        for item in getattr(card, field, ()) or ():
            lines.append(f"{tag}: {item}")
    for field in ("entities", "new_entities"):
        for entity in getattr(card, field, ()) or ():
            lines.append(f"ENTITY: {entity.name} | {entity.kind.value}")
    return "\n".join(lines)


class ScriptedModel:
    """FakeLLM handler that returns a scripted response keyed by schema."""

    def __init__(self) -> None:
        self.responses: dict[type[BaseModel] | None, Any] = {
            card_prompts.CardDraft: card_prompts.CardDraft(
                title="아폴로 지원 계약서",
                summary="대한물산과 유엔진 간의 아폴로 사업 유지보수 계약.",
                doc_type="계약서",
                language="ko",
                topics=["아폴로", "유지보수", "2023"],
                entities=[Entity(name="아폴로", kind=EntityKind.PROJECT)],
                keywords=["계약", "24개월"],
                answers_questions=["아폴로 계약 기간은?"],
            ),
            card_prompts.CardUpdate: card_prompts.CardUpdate(
                summary="대한물산과 유엔진 간의 아폴로 사업 유지보수 계약. 지연배상 조항을 포함한다.",
                new_topics=["지연배상"],
                new_entities=[Entity(name="대한물산", kind=EntityKind.ORGANIZATION)],
                new_keywords=["지연배상금"],
                new_questions=["지연배상금 요율은?"],
            ),
            card_prompts.DensifiedSummary: card_prompts.DensifiedSummary(
                summary="대한물산과 유엔진 간 아폴로 유지보수 계약. 기간 24개월, 지연배상 조항 포함.",
            ),
            placement_prompts.PlacementDecision: placement_to("아폴로/2023"),
            # Default: nothing has grown out yet. A test that wants a folder scripts it,
            # so every other test keeps the tree its assertions were written against.
            subdivision_prompts.Emerging: subdivision_prompts.Emerging(
                emerged=False,
            ),
            subdivision_prompts.Members: subdivision_prompts.Members(
                document_ids=[],
            ),
            # Emergence is four calls now. Tests still script the one Emerging they mean;
            # each step is answered from the field it is about, so an existing test keeps
            # asserting what it was written to assert.
            subdivision_prompts.Gathered: lambda prompt, schema: subdivision_prompts.Gathered(
                members=_subset_shown(prompt) if _scripted_emergence(self).emerged else [],
                # Cut to the schema's ceiling: a test that scripts an essay-length
                # SIGN is about the sign, and this field is only standing in for the
                # sentence the grouping step would have written.
                shared=(_scripted_emergence(self).sign or "같은 종류의 문서")[:300],
            ),
            subdivision_prompts.ClassName: lambda prompt, schema: subdivision_prompts.ClassName(
                name=_scripted_emergence(self).name,
            ),
            subdivision_prompts.ClassSign: lambda prompt, schema: subdivision_prompts.ClassSign(
                sign=_scripted_emergence(self).sign,
            ),
            subdivision_prompts.Axis: lambda prompt, schema: subdivision_prompts.Axis(
                axis=_scripted_emergence(self).axis,
                axis_question=_scripted_emergence(self).axis_question,
            ),
            # Same default, for the same reason: existing folders stay where they are
            # unless a test asks for them to be stood together.
            subdivision_prompts.Grouping: subdivision_prompts.Grouping(emerged=False),
            # Default: the whole-collection pass proposes nothing, so a test that is
            # not about it never redraws the tree its assertions were written against.
            redesign_prompts.Design: redesign_prompts.Design(),
            subdivision_prompts.ExistingAssignments: subdivision_prompts.ExistingAssignments(
                groups=[],
            ),
            charter_prompts.CharterDraft: charter_prompts.CharterDraft(
                purpose="아폴로 사업의 2023년 문서를 모아둡니다.",
            ),
        }
        self.responses[None] = self.responses.pop(placement_prompts.PlacementDecision)
        # Default: nothing joins a newly named class, matching the Emerging default.
        self._members: set[str] = set()
        self._routes: dict[str, str] = {}
        self._shelved: set[str] = set()
        self._dissolve: set[str] = set()
        self._axis_fails = False
        self._shelf_is_container = False
        self._name_is_beside = False
        self._assigned: dict[str, str] = {}

    def set(self, schema: type[BaseModel], response: object) -> None:
        key = None if schema is placement_prompts.PlacementDecision else schema
        self.responses[key] = response

    def set_choice(self, response: object) -> None:
        self.responses[None] = response

    def set_members(self, document_ids: list[str]) -> None:
        """Script closed-choice membership in a newly named class (ADR-0014).

        Membership stopped being a JSON list of ids and became one SHELF/STAY question
        per document, so scripting ``Members`` alone no longer reaches this path.
        """
        self._members = set(document_ids)

    def set_dissolve(self, paths: list[str]) -> None:
        """Script which levels answer DISSOLVE when asked whether they earn their guess."""
        self._dissolve = set(paths)

    def set_axis_fails(self, fails: bool = True) -> None:
        """Script the one check on the property a folder is about to be fixed on."""
        self._axis_fails = fails

    def set_shelf_is_container(self, container: bool = True) -> None:
        """Script the one check on a broader name before folders are moved under it."""
        self._shelf_is_container = container

    def set_name_is_beside(self, beside: bool = True) -> None:
        """Script the one check on whether a name answers the folder's question."""
        self._name_is_beside = beside

    def set_assigned(self, by_subject: dict[str, str]) -> None:
        """Script where a redesign puts each folder, as ``{folder name: C###}``."""
        self._assigned = dict(by_subject)

    def set_shelved(self, folder_names: list[str]) -> None:
        """Script which existing sub-folders move onto a proposed broader shelf."""
        self._shelved = set(folder_names)

    def set_routes(self, by_document: dict[str, str]) -> None:
        """Script routing a loose document into an existing direct child, as ``{id: F###}``."""
        self._routes = dict(by_document)

    def __call__(self, prompt: Prompt, schema: type[BaseModel] | None) -> BaseModel | str:
        if schema is None and (card := self._card(prompt)) is not None:
            return card
        if schema is None and (choice := self._plain_choice(prompt)) is not None:
            return choice
        try:
            response = self.responses[schema]
            return response(prompt, schema) if callable(response) else response
        except KeyError as exc:  # pragma: no cover
            wanted = schema.__name__ if schema is not None else "PlainChoice"
            raise AssertionError(f"nothing scripted for {wanted}") from exc

    def _card(self, prompt: Prompt) -> str | None:
        """Reading a document is open text now, so it arrives with no schema to key on."""
        if "일반 텍스트 줄" not in prompt.system:
            return None
        scripted = self.responses[
            card_prompts.CardUpdate
            if "SUMMARY 는 반드시 있어야 한다" in prompt.system
            else card_prompts.CardDraft
        ]
        if callable(scripted):
            scripted = scripted(prompt, None)
        return scripted if isinstance(scripted, str) else _as_lines(scripted)

    def _plain_choice(self, prompt: Prompt) -> str | None:
        """Route one closed choice to its own script.

        Several different questions share the plain-choice call and are only told apart
        by what they offer: a placement descent, membership in a new class, routing into
        an existing sign, dissolving a level, moving onto a shelf, checking a property.
        One shared slot sent them all to the placement chooser.
        """
        if "THE NEW TOP-LEVEL FOLDERS:" in prompt.user:
            # Placing one folder, or one loose document, under a redrawn top level.
            # STAY by default, matching production: a thing nothing claims keeps the
            # place it has.
            subject = prompt.user.split("WHAT IS BEING PLACED: ", 1)[-1].splitlines()[0]
            return self._assigned.get(subject.strip(), "STAY")
        if "THE PROPOSED NAME: " in prompt.user:
            # Whether a name answers the question its siblings answer. ANSWERS by
            # default, so every other test keeps the tree its assertions expect.
            return "BESIDE" if self._name_is_beside else "ANSWERS"
        if "THE BROADER NAME: " in prompt.user:
            # Whether a broader name is a class or a word for what the documents are.
            # CLASS by default, so the tests about grouping itself keep working.
            return "CONTAINER" if self._shelf_is_container else "CLASS"
        if "QUESTION IT ASKS: " in prompt.user:
            # The property a folder is about to be divided on. HOLDS by default, so the
            # tests that are about something else keep the trees their assertions expect.
            return "FAILS" if self._axis_fails else "HOLDS"
        if "THE LEVEL IN QUESTION: " in prompt.user:
            # Dissolving a level is asked about a folder, not a document, so it answers
            # from its own script. KEEP by default: a test that wants a level removed
            # says so, and every other test keeps the tree its assertions were written
            # against.
            level = prompt.user.split("THE LEVEL IN QUESTION: ", 1)[-1].splitlines()[0]
            return "DISSOLVE" if level in self._dissolve else "KEEP"
        if "THE FOLDER THAT WOULD MOVE INSIDE IT:" in prompt.user:
            # Whether a folder that already stands answers for what would move inside it.
            return "WIDER" if self._name_is_beside else "COVERS"
        if "THE BROADER SHELF:" in prompt.user:
            # Standing existing folders on one shelf: the choice is about a folder, not
            # a document, so it is answered from its own script.
            name = prompt.user.split("THE FOLDER IN QUESTION: ", 1)[-1].split("/", 1)[0]
            return "SHELF" if name in self._shelved else "STAY"
        document_id = _shown_document(prompt)
        if "NEW SIGN:" in prompt.user:
            return "SHELF" if document_id in self._members else "STAY"
        if "CURRENT FOLDER:" not in prompt.user and "\n  [F" in prompt.user:
            # Routing a loose document into an existing sign. Shares the F### handle
            # shape with a placement descent, so the descent marker is what tells them
            # apart. Default STAY: a document stays put unless a test says otherwise.
            return self._routes.get(document_id or "", "STAY")
        return None


@pytest.fixture
def script() -> ScriptedModel:
    return ScriptedModel()


@pytest.fixture
def llm(script: ScriptedModel) -> FakeLLM:
    return FakeLLM(handler=script)


SCRIPTED_FOLDER = PurePosixPath("아폴로/2023")


def seed_folder(root: Path, folder: PurePosixPath = SCRIPTED_FOLDER) -> None:
    """Put a folder on disk so placement has somewhere to choose.

    Placement chooses; it does not invent (see prompts/placement.py). A scripted
    decision naming a folder that does not exist is read as the root, so a test that
    wants a document filed somewhere has to put the somewhere there first.
    """
    target = root / Path(*folder.parts)
    target.mkdir(parents=True, exist_ok=True)
    note = Charter(path=folder, title=folder.name, purpose="아폴로 사업의 2023년 문서.")
    (target / CHARTER_FILENAME).write_text(note.to_markdown(), encoding="utf-8")


@pytest.fixture
def engine(settings: Settings, llm: FakeLLM) -> Bismuth:
    engine = build(settings, llm=llm)
    seed_folder(Path(engine.vault.root))
    return engine


@pytest.fixture
def client(settings: Settings, llm: FakeLLM) -> Iterator[TestClient]:
    # Wider than the product's own list on purpose: these tests are about upload,
    # placement and undo, and a real PDF fixture would only add bytes to read. What the
    # product accepts is checked on its own, against the default, in test_api.
    app = create_app(settings, accepted_uploads=frozenset({".pdf", ".txt", ".md", ".csv"}))
    engine = build(settings, llm=llm)
    seed_folder(Path(engine.vault.root))
    app.state.engine = engine
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def make_document(vault_path: Path) -> Callable[[str, str], Path]:
    def _make(name: str, body: str = "아폴로 지원 계약서, 2023.") -> Path:
        path = vault_path.parent / "incoming" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    return _make
