"""Two questions, and nothing else.

The whole job is a folder tree an agent can walk with ``ls``. That needs two decisions and
this module asks for exactly those:

* **where do these documents go**, asked of a handful at a time so the answer can see a
  class where a single document only shows a title;
* **is this tree still worth walking**, asked when the collection has grown enough that
  the answer could differ.

Both replies are plain tagged lines. A grammar compiled from a schema was measured to cost
the answer rather than shape it (docs/prior-art.md), and a line per document is the
smallest thing that can be parsed without one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from bismuth.ports.llm import Prompt

_FILING = """\
너는 문서를 폴더 트리에 배치한다. 이 트리를 읽는 것은 오직 `ls`, `grep`, `read` 만 가진 \
에이전트다. 폴더를 나열하면 그 안의 폴더 이름들이 보이고, 하나도 열어보지 않은 채 \
그중 하나를 골라야 한다. 폴더 이름이 그 에이전트가 가진 유일한 단서다.

지금 서 있는 트리와 문서 몇 건을 보여줄 테니, 각 문서를 어딘가에 두어라.

보여주는 형식은 이렇다. 이미 있는 폴더는 한 줄씩:

  <경로>/  (<그 폴더 자체에 놓인 문서 수>) — <무엇을 담는다고 적혀 있는지>

괄호 안 숫자는 **그 폴더에 직접 놓인 문서 수**이고 하위 폴더에 들어 있는 것은 세지 않는다. \
그래서 하위 폴더를 여럿 거느리고도 이 숫자가 큰 폴더는, 하위 폴더들이 별로 걸러주지 \
못하고 있는 폴더다. 뒤에 문장이 없는 폴더는 아직 설명이 붙지 않은 것이다.

배치할 문서도 한 줄씩:

  [D<번호>] <문서 자신의 제목> | <어떤 종류의 문서인지> | <무엇에 관한 것인지 몇 가지>

번호는 네 답에서 그 문서를 가리키는 이름일 뿐이고 이 요청 밖에서는 아무 뜻이 없다. \
이 줄들은 문서 자체가 아니라 **문서에서 읽어낸 것**이다. 그러니 네가 견주는 것은 본문이 \
아니라 주제다.

문서마다 이 순서로 판단하라.

**1. 이미 있는 폴더 중에 이 문서를 담는 곳이 있나?** 있으면 그게 답이다. 문서마다 폴더가 \
하나씩 생기는 트리는 항목마다 클릭이 하나 더 붙은 같은 목록일 뿐이고, 이미 서 있는 \
폴더는 이런 문서에게 집이 있다는 증거다.

**2. 없으면, 눈앞의 다른 문서들을 봐라.** 이들은 함께 도착했고 지금 이 순간에만 한꺼번에 \
볼 수 있다. 둘 이상이 함께 속한다면 그게 폴더다. 이름을 붙이고 거기에 넣어라. 새 폴더는 \
대부분 여기서 나와야 한다.

**3. 그다음에야 루트다.** ROOT 는 "여기 있는 어느 폴더도 이 문서를 담지 않고, 이번에 함께 \
온 문서 중에도 짝이 없다" 는 뜻이다. 참일 때는 그렇게 답해야 하는 정당한 답이지만, \
안전한 답이 아니라 **마지막 수단**이다. 전부 ROOT 라고 답한 배치는 아무것도 정하지 않은 \
것이고, 그렇게 남은 더미는 읽는 사람이 트리 대신 읽어야 할 목록이 된다.

**폴더 이름은 그 문서들이 무엇에 관한 것인지를 말한다.** 문서가 무엇인지가 아니다 — \
형식, 종류, 날짜, 발행 주체, 법령의 위계 같은 것은 거의 모든 문서에 해당해서 아무것도 \
가르지 못한다. 장서 대부분에 참인 이름은 아무것도 배제하지 못한다.

**"기타", "일반", "관련", "그 외" 같은 이름은 쓰지 않는다.** 안에 뭐가 있는지 알 수 없고, \
나중에 오는 것이 전부 거기 들어맞는다.

아직 없는 경로를 지어도 되고, 중첩해도 된다. `금융/은행법` 은 `금융` 안에 폴더를 만든다. \
다만 얕게 유지하라 — 층이 하나 늘 때마다 읽는 사람이 맞혀야 할 선택이 하나 늘고, 어느 \
한 층에서 틀리면 찾던 것에 영영 닿지 못한다.

답하기 전에 문서들끼리 한 번 대조하라. 같은 것을 다루는 둘은, 그곳이 어디로 정해지든 \
같은 곳에 있어야 한다.

답은 문서당 한 줄이고, 그 외에는 아무것도 쓰지 마라:

D1: <폴더 경로, 또는 ROOT>
D2: <폴더 경로, 또는 ROOT>

위 목록에 없던 경로를 새로 지었다면, 그 폴더가 무엇을 담는지 한 줄씩 덧붙여라. 이 폴더를 \
처음 보는 사람, 안에 든 문서를 볼 수 없는 사람에게 설명하듯 써라:

SIGN: <폴더 경로> | <한 문장>\
"""

_REVIEW = """\
너는 에이전트가 `ls` 로 걸어다니는 폴더 트리를 보고 있다. 이 트리가 아직 걸어다닐 만한지 \
판단하고 그대로 말하라.

폴더는 한 줄씩 보여준다:

  <경로>/  (<그 폴더 자체에 놓인 문서 수>) — <무엇을 담는다고 적혀 있는지>

괄호 안 숫자는 **그 폴더에 직접 놓인 문서 수**이고 하위 폴더에 들어 있는 것은 세지 않는다. \
그래서 자기 숫자가 아래에 정리된 것 전부보다 큰 폴더는, 실은 아무것도 나누지 못한 폴더다.

읽는 사람은 폴더를 나열하고, 그 안의 이름들을 보고, 하나도 열지 않은 채 하나를 고른다. \
그러니 각 층의 이름들이 그 아래 있는 것을 갈라줄 때 트리가 제 일을 하는 것이고, 이름들을 \
서로 구별할 수 없을 때, 한 폴더가 장서 대부분을 안고 있을 때, 어느 폴더의 자기 더미가 \
그 아래 정리된 것 전부보다 클 때, 같은 주제가 두 곳에 나뉘어 있을 때 실패한 것이다.

깊이는 공짜가 아니다. 층 하나하나가 맞혀야 할 선택이고 한 번 틀리면 회복되지 않는다. \
그에 비하면 폭은 싸다 — 뜻이 분명한 이름 열두 개를 한 번에 훑는 것은 판단 한 번이고, \
아무 뜻 없는 이름 하나가 뜻 있는 이름 열 개보다 나쁘다.

트리가 충분히 괜찮으면 그렇다고 말하고 끝내라. **어설픈 트리가, 물어볼 때마다 다시 그리는 \
트리보다 낫다.** 다시 그릴 때마다 읽는 사람이 이미 익힌 자리에서 문서가 옮겨지기 때문이다.

괜찮지 않다면 무엇을 옮길지 말하라. 폴더를 다른 폴더 아래로 옮기거나, 새 경로로 옮겨 \
이름을 바꾸거나, 루트로 끌어올릴 수 있다. 문서는 자기가 있는 폴더를 따라 움직인다.

답은 정확히 이것이거나:

KEEP

이동 목록이고, 그 외에는 아무것도 쓰지 마라:

MOVE: <지금 있는 경로> | <가야 할 곳>
SIGN: <새 경로> | <무엇을 담는지 한 문장>\
"""


@dataclass(frozen=True, slots=True)
class Folder:
    """One line of the tree as the model is shown it."""

    path: PurePosixPath
    note: str
    documents: int
    """Sitting directly in it, which is what says whether its children are doing any work."""


def _tree(folders: list[Folder]) -> str:
    if not folders:
        return "  (아직 폴더가 없다. 전부 루트에 있다.)"
    lines = []
    for folder in sorted(folders, key=lambda f: str(f.path)):
        depth = len(folder.path.parts) - 1
        held = f" — {folder.note}" if folder.note else ""
        lines.append(f"  {'  ' * depth}{folder.path}/  ({folder.documents}건){held}")
    return "\n".join(lines)


def build_filing(
    *,
    folders: list[Folder],
    documents: list[tuple[str, str]],
    loose: int,
    language: str = "",
) -> Prompt:
    """Where this handful of documents goes, in one call.

    A handful rather than one, because a class is only visible in several: asked about a
    single document the only honest answer is its title, and a tree of titles is the list
    the folders were supposed to replace.
    """
    listed = "\n".join(f"  [{handle}] {line}" for handle, line in documents)
    say = (
        f"이 문서들은 `{language}` 로 쓰여 있다. 폴더 이름과 설명도 문서가 쓰는 말을 "
        f"그대로 써서 `{language}` 로 적어라.\n\n"
        if language
        else ""
    )
    return Prompt(
        system=_FILING,
        user=(
            f"{say}이미 있는 폴더:\n{_tree(folders)}\n\n"
            f"루트에 놓인 채 아직 어디에도 안 들어간 문서: {loose}건\n\n"
            f"배치할 문서 ({len(documents)}건):\n{listed}"
        ),
    )


def build_review(*, folders: list[Folder], total: int, loose: int, language: str = "") -> Prompt:
    """Whether the tree is worth walking, asked when it has grown enough to answer differently."""
    say = (
        f"폴더 이름이나 설명을 쓸 일이 있으면 문서가 쓰는 말을 그대로 써서 "
        f"`{language}` 로 적어라.\n\n"
        if language
        else ""
    )
    return Prompt(
        system=_REVIEW,
        user=(
            f"{say}이 장서는 문서 {total}건을 담고 있다.\n"
            f"그중 {loose}건은 루트에 놓인 채 아직 어디에도 안 들어갔다.\n\n"
            f"트리:\n{_tree(folders)}"
        ),
    )


def parse_filing(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """``{handle: path}`` and ``{path: sign}`` from the reply.

    Unrecognised lines are dropped rather than guessed at. A handle nobody asked about, or
    a document with no line, is the caller's problem to notice -- this only reads.
    """
    placed: dict[str, str] = {}
    signs: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        tag, separator, value = line.partition(":")
        tag, value = tag.strip(), value.strip()
        if not separator or not value:
            continue
        if tag.upper() == "SIGN":
            path, _, sentence = value.partition("|")
            if path.strip() and sentence.strip():
                signs[path.strip().strip("/")] = sentence.strip()
        elif tag.upper().startswith("D") and tag[1:].isdigit():
            placed[tag.upper()] = value
    return placed, signs


def parse_review(text: str) -> tuple[bool, list[tuple[str, str]], dict[str, str]]:
    """``(keep, [(from, to)], {path: sign})``.

    ``keep`` is the answer, not the absence of one: a reply with no moves in it leaves the
    tree alone, which is the same thing said two ways.
    """
    moves: list[tuple[str, str]] = []
    signs: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        tag, separator, value = line.partition(":")
        tag, value = tag.strip().upper(), value.strip()
        if not separator or not value:
            continue
        if tag == "MOVE":
            source, _, target = value.partition("|")
            if source.strip() and target.strip():
                moves.append((source.strip().strip("/"), target.strip().strip("/")))
        elif tag == "SIGN":
            path, _, sentence = value.partition("|")
            if path.strip() and sentence.strip():
                signs[path.strip().strip("/")] = sentence.strip()
    return not moves, moves, signs
