"""Filing asked as two questions instead of one.

One call was asked to decide containment, invent a name, respect a size, consider nesting
and choose between the root and a new folder -- and it settled on the answer that satisfies
all of those at once and organises nothing: one folder per document family. Measured on 300
Korean statutes, 45 of 59 top-level folders held a single law and its decrees, and no reply
in 300 ever named a nested path.

So the question is split where the decisions actually differ:

* **which part of the tree is this near** -- answered against the whole tree, for a batch
  at a time, with no obligation to be right about what happens next;
* **what should happen there** -- answered against one neighbourhood at a time, seeing the
  documents already in it, and allowed to build a parent over folders that turned out to be
  siblings. That last one is the operator whose absence the measurement found.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from bismuth.ports.llm import Prompt
from bismuth.prompts.simple import Folder, _size, _tree

_NEAREST = """\
너는 문서 몇 건이 폴더 트리의 **어디쯤에** 속하는지만 고른다. 실제로 넣거나 폴더를 만드는 \
일은 다음 단계에서 한다. 지금은 자리만 짚는다.

트리와 문서를 보여줄 테니, 문서마다 **가장 가까운 폴더 하나**를 골라라.

**"가장 가까운" 은 "딱 들어맞는다" 가 아니다.** 이 문서와 같은 갈래를 다루는 폴더가 있으면 \
그걸 고르면 된다. 그 폴더에 그대로 들어갈지, 그 아래 하위를 만들지, 그 폴더와 이 문서를 \
함께 덮는 새 폴더를 지을지는 **다음 단계에서** 그 폴더 안에 든 문서까지 보고 정한다. \
그러니 완벽하지 않다고 `없음` 을 고르지 마라.

정말로 어느 폴더와도 갈래가 다를 때만 `없음` 이다.

보여주는 형식은 이렇다. 폴더는 한 줄씩:

  <경로>/  (<그 폴더가 담고 있는 전부>건, 그중 여기 직접 <n>건, 하위 <k>개) — <설명>

하위 폴더가 없는 폴더는 그냥 `(<n>건)` 으로만 적혀 있다. **앞의 숫자가 그 폴더 아래 있는 전부다.** 이미 갈라진 큰 폴더는 자기 자리에 든 것이 적을 수 있지만, 그건 빈 폴더라는 뜻이 아니라 잘 정리돼 있다는 뜻이다.

문서도 한 줄씩:

  [D<번호>] <문서 자신의 제목> | <어떤 종류의 문서인지> | <무엇에 관한 것인지 몇 가지>

답은 문서당 한 줄이고, 그 외에는 아무것도 쓰지 마라:

D1: <폴더 경로, 또는 없음>
D2: <폴더 경로, 또는 없음>\
"""

_SHAPING = """\
너는 폴더 트리를 손본다. 트리를 읽는 것은 `ls` 만 가진 에이전트이고, 폴더 이름이 그가 가진 \
유일한 단서다.

**보여주는 것에는 두 종류의 번호가 붙는다.** `[F1]` 은 **폴더**, `[D1]` 은 **문서**다. \
답할 때 폴더 자리에는 반드시 `F` 번호를, 문서 자리에는 `D` 번호를 써라. 이름을 그대로 \
옮겨 적지 마라 -- 문서 제목을 폴더 자리에 쓰면 아무 일도 일어나지 않는다.

**여기로 왔다고 해서 여기 넣어야 하는 것은 아니다.** 앞 단계는 "가장 가까운 자리"를 고른 \
것이지 "맞는 자리"를 고른 것이 아니다. 그러니 먼저 시험하라:

  그 폴더에 적혀 있는 것을 읽은 사람이, 이 문서가 거기 있으리라고 **예상하는가?**

예상하지 못하면 그 폴더가 아니다. 다른 폴더가 더 맞으면 그쪽으로 `안에` 하고, 어디에도 \
안 맞으면 `루트` 로 돌려라. 잘못 들어간 문서는 읽는 사람이 영영 못 찾는다 -- 억지로 넣는 \
것보다 루트에 남기는 편이 낫다.

자리마다 할 수 있는 일은 넷이다.

**안에 넣는다.** 이번 문서가 그 폴더가 담는 것에 그대로 속하고, 폴더가 아직 크지 않을 때. \
자리 머리에 그 폴더가 지금 몇 건을 직접 들고 있는지 적어 두었다. **25건이 넘어가면 그냥 \
넣지 말고 하위를 만들어라** -- 25건이 넘는 폴더는 열었을 때 폴더 이름이 아니라 문서 목록을 \
마주하게 한다.

**하위를 만든다.** 그 폴더 안에서 이번 문서들이 따로 묶일 때. 하위 하나에는 **둘 이상**이 \
들어가야 한다. 한 건짜리 하위는 클릭만 늘리고 아무것도 걸러주지 않는다.

**이름을 바꾼다.** 폴더 이름이 지금 그 안에 든 것을 더 이상 말해주지 않을 때.

**새로 묶는다.** 어느 폴더에도 안 들어가는 문서 **둘 이상**이 같은 것을 다룰 때, 이름을 \
지어 함께 넣는다.

한 번에 여럿을 해도 된다. 늘어선 폴더들을 한 부모 아래로 모으는 일은 여기서 하지 않는다 -- \
그건 따로 묻는다.

**이름을 지을 때.** 이름은 그 안의 문서들이 **무엇에 관한 것인지**를 말한다. 문서가 무엇인지 \
(형식·종류·날짜·발행 주체·법령의 위계)가 아니다. 그리고 **법 하나의 이름은 폴더 이름이 아니다** \
-- 그 법과 그 시행령·시행규칙만 들어갈 수 있는 이름이면, 그건 분류가 아니라 색인이다. \
짓기 전에 물어라: **이 이름이 거짓인 문서를 이 장서에서 하나 댈 수 있는가?** 그리고 \
**앞으로 올 다른 문서도 여기 들어올 수 있는가?** \
"기타", "일반", "관련", "그 외" 는 쓰지 않는다.

답은 아래 줄들뿐이고, 그 외에는 아무것도 쓰지 마라. 필요한 만큼 되풀이하라:

안에: F2 | D1, D3
하위: F2 | <새 하위 이름> | D2, D5
이름: F2 | <새 이름>
넣기: <새 폴더 이름> | D4, D6
루트: D7, D9
SIGN: <새로 지은 이름> | <무엇을 담는지 한 문장>

`넣기` 는 아무 폴더에도 안 들어가는 문서들끼리 새로 묶을 때 쓰고, 둘 이상일 때만 이름을 \
짓는다. `루트` 는 아직 어디에도 속하지 않는 문서이고, 정당한 답이다.\
"""

_GROUPING = """\
너는 폴더 목록만 본다. **문서는 하나도 보지 않는다.** 지금 이 층에 폴더가 너무 많이 늘어서 \
있어서, 읽는 사람이 목록을 처음부터 끝까지 읽어야 하는 상태다. 이름 쉰 개가 늘어선 목록은 \
폴더가 아니라 색인이다.

같은 갈래인 폴더들을 찾아 **그것들을 덮는 이름**을 짓고 그 아래로 묶어라.

규칙은 넷이다.

1. **한 묶음에 폴더가 둘 이상**이어야 한다. 하나짜리는 층만 늘리고 아무것도 걸러주지 않는다.
2. **묶는 폴더는 `F` 번호로 가리킨다.** 이름을 옮겨 적지 마라. 번호를 정확히 세어라.
3. **새 이름은 그 안에 들어갈 폴더들을 전부 덮되, 그 이상은 덮지 않아야 한다.** 너무 넓으면 \
나중에 오는 것이 전부 거기 들어가고, 너무 좁으면 안에 든 것 중 일부가 이름과 어긋난다.
4. **어울리지 않는 폴더는 그냥 두어라.** 전부 묶을 필요는 없다. 억지로 묶은 하나가 잘 묶은 \
다섯을 버린다 -- 읽는 사람은 엉뚱한 곳에 들어간 폴더를 영영 못 찾는다.
5. **이미 있는 폴더를 부모로 삼아도 된다.** 목록의 어느 이름이 나머지 몇을 이미 잘 덮고 \
있다면, 새 이름을 짓지 말고 그 이름을 그대로 부모 자리에 쓰면 된다. 다만 그 폴더 자신을 \
`F` 번호 목록에 함께 넣지는 마라 -- 자기 아래로 자기를 넣을 수는 없다. **큰 폴더 옆에 \
두세 건짜리 폴더가 몇 개 남아 있다면, 대개 그중 몇은 그 큰 폴더 안에 들어갈 것들이다.** \
그리고 **이미 있는 이름도 새로 짓는 이름과 똑같은 시험을 통과해야 한다** -- 문서 하나의 \
제목으로 지어진 이름은 그 문서의 자리이지 여러 갈래를 덮는 이름이 아니다. 그런 폴더를 \
부모로 삼지 말고, 그것까지 함께 덮는 이름을 새로 지어라.
6. **부모 이름이 자식 하나의 이름과 사실상 같은 말이면 만들지 마라.** 층을 하나 더 지나야 \
같은 뜻을 다시 읽게 되는 것뿐이고, 읽는 사람은 아무것도 얻지 못한 채 클릭만 한 번 더 한다. \
부모 이름은 그 아래 것들을 **함께** 덮되, 그중 어느 하나로 바꿔 쓸 수는 없어야 한다.
7. **묶기 전에 괄호 안 숫자를 더해 보라.** 새 부모가 담게 될 문서가 이 층 전체의 절반을 \
넘는다면 그건 범주가 아니라 서고 전체다. 그런 이름 아래에서는 읽는 사람이 아무것도 \
좁히지 못하고, 다음에 오는 문서도 대부분 거기로 간다. 서로 다른 두 갈래를 억지로 한 \
이름으로 덮고 있는 것은 아닌지 보라 -- 이름에 "및" 이나 "과" 를 넣어야 뜻이 이어진다면 \
대개 그 둘은 따로 서야 할 갈래다.

**이름을 지을 때.** 이름은 그 안에 든 것이 **무엇에 관한 것인지**를 말한다. 그것이 무엇인지가 \
아니다 -- 형식, 종류, 날짜, **만든 조직이나 발행 주체**는 주제가 아니다. 어떤 조직이 만든 \
것들이라는 이유로 묶으면, 그 조직이 관장하는 온갖 주제가 한 이름 아래 섞이고 읽는 사람은 \
그 이름에서 아무것도 짐작하지 못한다.

지은 이름에 시험 둘을 걸어라.

1. **이 이름이 거짓인 문서를 이 장서에서 하나 댈 수 있는가?** 못 댄다면 아무것도 배제하지 \
못하는 이름이다.
2. **자식 이름을 전부 가리고 부모 이름만 읽었을 때, 그 자식들이 거기 있으리라고 예상되는가?** \
하나라도 예상 밖이면 그 자식은 그 묶음에서 빼라.

"기타", "일반", "관련", "그 외" 는 쓰지 않는다.

묶을 것이 없으면 `없음` 한 줄만 쓰고 끝내라. 있으면 아래 두 줄만 쓰고, 그 외에는 아무것도 \
쓰지 마라:

묶기: <새 이름> | F1, F3, F7
SIGN: <새 이름> | <무엇을 담는지 한 문장>\
"""


def build_nearest(
    *,
    folders: list[Folder],
    documents: list[tuple[str, str]],
    language: str = "",
) -> Prompt:
    """Which part of the tree each document is near. One call for the batch."""
    say = f"이 문서들은 `{language}` 로 쓰여 있다.\n\n" if language else ""
    listed = "\n".join(f"  [{handle}] {line}" for handle, line in documents)
    return Prompt(
        system=_NEAREST,
        user=(
            f"{say}지금 서 있는 트리:\n{_tree(folders)}\n\n"
            f"자리를 짚을 문서 ({len(documents)}건):\n{listed}"
        ),
    )


@dataclass(frozen=True, slots=True)
class Place:
    """One neighbourhood as the second question is shown it."""

    folder: PurePosixPath
    note: str
    holding: list[str]
    """What is already in it, one line per document -- the first few, not all of them."""

    held: int
    """How many it really holds. Shown twelve of thirty-two and told nothing else, the
    reply reads a folder over its size as a folder well under it, and the rule about size
    can never fire."""

    children: list[str]
    arriving: list[tuple[str, str]]
    """The documents this batch sent here."""


def build_shaping(
    *,
    folders: list[Folder],
    places: list[Place],
    homeless: list[tuple[str, str]],
    language: str = "",
) -> Prompt:
    """What to do at each place: put in, divide, build a parent over, or rename.

    Every folder is listed once with a handle, and the places refer to it by that handle.
    Folders and documents are told apart by the letter, which is the whole point: asked to
    name folders in prose, the reply named documents instead and nothing happened.
    """
    handles = {str(folder.path): f"F{index}" for index, folder in enumerate(folders, start=1)}
    listing = (
        "\n".join(
            f"  [{handles[str(folder.path)]}] {folder.path}/  {_size(folder)}"
            + (f" — {folder.note}" if folder.note else "")
            for folder in folders
        )
        or "  (아직 폴더가 없다)"
    )
    blocks = [f"지금 서 있는 폴더:\n{listing}"]
    for place in places:
        held = "\n".join(f"    - {line}" for line in place.holding) or "    (아직 없다)"
        if place.held > len(place.holding):
            held += f"\n    … 외 {place.held - len(place.holding)}건 (여기까지만 보여준다)"
        kids = ", ".join(place.children) or "없음"
        coming = "\n".join(f"    [{handle}] {line}" for handle, line in place.arriving)
        mine = handles.get(str(place.folder), "?")
        blocks.append(
            f"■ 자리 [{mine}] {place.folder}/  "
            f"— 이 폴더가 직접 들고 있는 문서 {place.held}건, 하위 폴더 {len(place.children)}개"
            + (f"\n  적혀 있는 것: {place.note}" if place.note else "")
            + f"\n  지금 이 폴더에 든 문서:\n{held}"
            + f"\n  하위 폴더: {kids}"
            + f"\n  이번에 여기로 온 문서:\n{coming}"
        )
    if homeless:
        listed = "\n".join(f"    [{handle}] {line}" for handle, line in homeless)
        blocks.append(
            "■ 자리: 루트 — 가까운 폴더가 없던 문서들.\n"
            "  위 목록의 폴더에 넣으려면, **이름이 아니라 그 옆에 적힌 설명**으로 판정하라. "
            "그 설명을 읽은 사람이 이 문서가 거기 있으리라고 예상하지 못하면 그 폴더가 "
            "아니다. 목록에는 그 폴더에 무엇이 들었는지가 안 보이므로, 이름만 그럴싸하다고 "
            "밀어 넣으면 그 폴더는 이름과 다른 것들로 채워진다.\n"
            "  괄호 안 숫자가 25를 넘는 폴더에는 넣지 마라. 이미 너무 크다.\n"
            "  아무 폴더도 맞지 않으면 `넣기` 로 새 이름을 짓거나(둘 이상일 때), "
            f"`루트` 로 남겨라. 루트에 남기는 것은 정당한 답이다.\n{listed}"
        )
    say = (
        f"이 문서들은 `{language}` 로 쓰여 있다. 폴더 이름도 그 말로 지어라.\n\n"
        if language
        else ""
    )
    return Prompt(system=_SHAPING, user=say + "\n\n".join(blocks))


_SETTLING = """\
너는 한 층에 늘어선 폴더들을 마지막으로 훑는다. 그중 몇은 여러 갈래를 담은 **큰 폴더**이고, \
몇은 갈래 하나뿐인 **작은 폴더**다. 물을 것은 하나뿐이다:

  **이 작은 폴더는 저 큰 폴더 안에 들어가는가?**

큰 폴더마다 그 안에 무엇이 들어 있는지 적어 두었다. 작은 폴더가 그 목록의 한 갈래로 \
읽힌다면 -- 그 큰 폴더를 열어본 사람이 이것도 거기 있으리라 예상한다면 -- 안으로 넣어라. \
층을 훑는 사람에게 갈래 하나짜리 폴더 열 개가 큰 갈래 다섯 개와 나란히 놓여 있으면, 그 \
열 개가 다섯 개를 덮어버린다.

**들어갈 곳이 없으면 그대로 두어라.** 그것이 정말 다른 갈래라면 최상위에 서 있는 것이 맞다. \
억지로 넣은 하나가 읽는 사람을 영영 헤매게 한다.

**작은 폴더끼리 묶어도 된다.** 큰 폴더 중에는 없지만 작은 것 **둘 이상**이 같은 갈래라면, \
그 둘을 덮는 이름을 지어 함께 묶어라.

번호는 `F` 로 가리킨다. 이름을 옮겨 적지 마라.

옮길 것이 없으면 `없음` 한 줄만 쓰고 끝내라. 있으면 아래 줄만 쓰고, 그 외에는 아무것도 \
쓰지 마라. 큰 폴더 안으로 넣을 때는 그 폴더의 `F` 번호를 부모 자리에 쓴다:

묶기: F6 | F2, F9
묶기: <새 이름> | F1, F8
SIGN: <새 이름> | <무엇을 담는지 한 문장>\
"""


def build_grouping(*, folders: list[Folder], settling: bool = False, language: str = "") -> Prompt:
    """Which of these folders are siblings under a name nobody has written yet.

    Folders only. Shown documents as well, the reply answered with document numbers where
    folder numbers were asked for -- 37 of 43 times -- because a parent is a natural thing
    to want for a document too. Taking the documents away takes the ambiguity with them.
    """
    listing = "\n".join(
        f"  [F{index}] {folder.path.name}/  {_size(folder)}"
        + (f" — {folder.note}" if folder.note else "")
        for index, folder in enumerate(folders, start=1)
    )
    where = folders[0].path.parent if folders and folders[0].path.parts[:-1] else None
    say = f"이 장서는 `{language}` 로 쓰여 있다. 이름도 그 말로 지어라.\n\n" if language else ""
    return Prompt(
        system=_SETTLING if settling else _GROUPING,
        user=(
            f"{say}{'`' + str(where) + '/` 아래' if where else '최상위'}에 폴더 "
            f"{len(folders)}개가 늘어서 있다:\n\n{listing}"
        ),
    )


def parse_grouping(
    text: str, folders: list[Folder]
) -> tuple[list[tuple[str, list[str]]], dict[str, str]]:
    """``[(new parent, folders under it)]`` and the sentences for the new names."""
    known = {f"F{index}": str(folder.path) for index, folder in enumerate(folders, start=1)}
    groups: list[tuple[str, list[str]]] = []
    signs: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        tag, separator, value = line.partition(":")
        tag, value = tag.strip(), value.strip()
        if not separator or not value:
            continue
        left, _, right = value.partition("|")
        # The parent may be a folder that already stands, named by its handle: a good name
        # earning more of the tree is the same operation as a new name being invented.
        name, right = known.get(left.strip().upper(), _named(left)), right.strip()
        if not name or not right:
            continue
        if tag == "묶기":
            under = [
                found for part in right.split(",") if (found := known.get(part.strip().upper()))
            ]
            # One is allowed here; whether it is enough depends on the parent existing,
            # which only the caller knows.
            if under:
                groups.append((name, under))
        elif tag.upper() == "SIGN":
            signs[name] = right
    return groups, signs


@dataclass(frozen=True, slots=True)
class Shaped:
    """What the second question asked for."""

    inside: dict[str, list[str]] = field(default_factory=dict)
    """``folder -> handles`` put straight in."""

    below: dict[str, list[str]] = field(default_factory=dict)
    """``new sub-folder path -> handles``."""

    made: dict[str, list[str]] = field(default_factory=dict)
    """``new folder path -> handles``, for documents that had no near place."""

    renamed: list[tuple[str, str]] = field(default_factory=list)
    loose: list[str] = field(default_factory=list)
    signs: dict[str, str] = field(default_factory=dict)


def parse_nearest(text: str) -> dict[str, str]:
    """``{handle: folder}``. An answer of 없음 is left out, which is the same as saying it."""
    near: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        handle, separator, value = line.partition(":")
        handle, value = handle.strip(), value.strip().strip("/")
        if not separator or not handle.upper().startswith("D") or not handle[1:].isdigit():
            continue
        if value and value not in ("없음", "NONE", "-", "ROOT"):
            near[handle.upper()] = value
    return near


def parse_shaping(text: str, folders: list[Folder]) -> Shaped:
    """The four moves, plus the two answers for documents that had no place.

    ``folders`` is the same list the question was built from, in the same order: that is
    what an ``F`` handle means. A folder named in prose instead is accepted when it matches
    one exactly, and dropped otherwise -- a name that resolves to nothing would silently
    become a new top-level folder, which is the failure this rewrite exists to stop.
    """
    known = {f"F{index}": str(folder.path) for index, folder in enumerate(folders, start=1)}
    paths = {str(folder.path) for folder in folders}

    def standing(value: str) -> str:
        """A folder that exists, by handle or by exact name. Empty when it is neither."""
        cleaned = value.strip().strip("/")
        if (found := known.get(cleaned.upper())) is not None:
            return found
        return cleaned if cleaned in paths else ""

    shaped = Shaped()
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        tag, separator, value = line.partition(":")
        tag, value = tag.strip(), value.strip()
        if not separator or not value:
            continue
        parts = [part.strip() for part in value.split("|")]
        left, right = parts[0].strip("/"), parts[1] if len(parts) > 1 else ""
        if tag == "안에" and (where := standing(left)) and right:
            shaped.inside.setdefault(where, []).extend(_handles(right))
        elif tag == "하위" and (where := standing(left)) and len(parts) > 2:
            below = f"{where}/{_named(parts[1])}"
            shaped.below.setdefault(below, []).extend(_handles(parts[2]))
        elif tag == "넣기" and (name := _named(left)) and right:
            shaped.made.setdefault(name, []).extend(_handles(right))
        elif tag == "이름" and (where := standing(left)) and (name := _named(right)):
            shaped.renamed.append((where, name))
        elif tag == "루트":
            shaped.loose.extend(_handles(value))
        elif tag.upper() == "SIGN" and left and right:
            shaped.signs[standing(left) or _named(left)] = right
    return shaped


def _named(value: str) -> str:
    """A name the reply invented. Handles are not names -- ``F2`` as a new folder is a slip."""
    cleaned = value.strip().strip("/").strip()
    if cleaned.upper().startswith(("F", "D")) and cleaned[1:].isdigit():
        return ""
    return cleaned


def _handles(value: str) -> list[str]:
    """``D1, D3`` -- and forgiving about how they are separated."""
    out = []
    for part in value.replace("/", ",").replace("·", ",").split(","):
        handle = part.strip().upper().strip(".")
        if handle.startswith("D") and handle[1:].isdigit():
            out.append(handle)
    return out
