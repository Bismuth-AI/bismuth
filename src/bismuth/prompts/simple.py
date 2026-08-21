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

from collections.abc import Mapping
from dataclasses import dataclass, field
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

문서마다 아래 세 갈래를 **나란히 놓고** 하나를 골라라. 앞의 것이 되면 뒤는 안 보는 순서가 \
아니다. 셋을 다 따져 보고 고르는 것이다.

**갈래 1 — 이미 있는 폴더.** 넣어도 되는지는 시험 하나로 정한다:

  그 폴더 설명을 읽은 사람이, 이 문서가 거기 있으리라고 **예상하는가?**

예상하지 못하면 그 폴더가 아니다. **"관련이 있다" 는 근거가 되지 않는다.** 관련은 어디에나 \
있고, 관련만으로 넣기 시작하면 이름이 넓은 폴더 하나가 장서를 통째로 삼킨다. 물어야 할 것은 \
포함이다 -- 이 문서는 그 설명이 말하는 것의 한 사례인가? 이를테면 어느 폴더가 "어떤 부처의 \
조직과 직제" 를 담는다고 적혀 있다면, 그 부처가 관장하는 다른 주제의 문서는 관련은 있어도 \
그 설명 안에 들어가지 않는다.

**이름과 설명이 어긋나면 설명을 따르라.** 폴더 이름은 짧아서 실제보다 넓어 보인다. 설명은 \
그 폴더가 실제로 무엇을 모아 두었는지 말한다. 이름이 넓다는 이유로 넣지 마라.

**갈래 2 — 새 폴더.** 이번에 함께 온 문서들끼리 견줘라. 이들은 지금 이 순간에만 한꺼번에 \
볼 수 있다. **둘 이상**이 같은 것을 다루면 그것이 폴더다. 이름을 붙이고 거기에 넣어라. \
하나뿐이면 폴더가 아니다 -- 문서 한 건짜리 폴더는 읽는 사람에게 클릭 하나를 물리고 아무것도 \
걸러주지 않는다.

**갈래 3 — ROOT.** 둘 다 아니면 루트다. "이 문서를 포함하는 폴더가 아직 없고, 이번에 함께 \
온 문서 중에도 짝이 없다" 는 뜻이며, 참일 때는 그렇게 답해야 하는 정당한 답이다. 억지로 \
어딘가에 끼워 넣는 것보다 낫다 -- 루트에 남은 문서는 나중에 점검이 다시 본다.

**이미 큰 폴더에는 하위로 넣어라.** 한 폴더가 **직접** 담는 문서는 25건을 넘지 않는 것이 \
목표다. 괄호 안 숫자가 바로 그 수다. 그보다 큰 폴더에 넣어야 하는데 이번 배치에서 **둘 \
이상**이 그 폴더 안의 같은 갈래에 속한다면, 하위 경로로 답하라. 혼자라면 그 폴더에 그냥 \
두어라 -- 그 폴더를 통째로 다시 정리할 때 함께 정리된다.

**폴더 이름은 그 문서들이 무엇에 관한 것인지를 말한다.** 문서가 무엇인지가 아니다 — \
형식, 종류, 날짜, 발행 주체, 법령의 위계 같은 것은 거의 모든 문서에 해당해서 아무것도 \
가르지 못한다. 이름을 짓기 전에 물어라: **이 이름이 거짓인 문서를 이 장서에서 하나 댈 수 \
있는가?** 못 댄다면 그 이름은 아무것도 배제하지 못하므로 이름이 아니다.

**폴더 이름이 그 안에 든 문서의 제목이면 그건 분류가 아니라 색인이다.** 문서 하나(와 그 \
문서에 딸린 것들)만 들어갈 수 있는 이름을 지으면 문서 수만큼 폴더가 생기고, 읽는 사람은 \
목록 하나를 목록 두 개로 바꿔 읽게 된다. 새로 짓는 이름은 **아직 오지 않은 문서도 들어올 \
수 있을 만큼** 넓어야 한다.

**"기타", "일반", "관련", "그 외" 같은 이름은 쓰지 않는다.** 안에 뭐가 있는지 알 수 없고, \
나중에 오는 것이 전부 거기 들어맞는다.

아직 없는 경로를 지어도 되고, 중첩해도 된다. `가/나` 는 `가` 안에 `나` 를 만든다. \
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

  <경로>/  (<아래 전부>건, 그중 여기 직접 <n>건, 하위 <k>개) — <무엇을 담는다고 적혀 있는지>

하위가 없는 폴더는 `(<n>건)` 으로만 적힌다. **"여기 직접" 이 그 폴더가 나누지 못하고 안고 \
있는 더미**이고, 그 수가 아래에 정리된 것보다 크면 그 폴더의 하위들은 아무것도 걸러주지 \
못하고 있는 것이다.

읽는 사람은 폴더를 나열하고, 그 안의 이름들을 보고, 하나도 열지 않은 채 하나를 고른다. \
그러니 각 층의 이름들이 그 아래 있는 것을 갈라줄 때 트리가 제 일을 하는 것이다.

**이 트리를 아홉 가지로 검사하라. 인상으로 판단하지 말고, 하나씩 세어 보고 답하라.**

**검사 1 — 자기 더미가 25건을 넘는 폴더가 있나?** 25건이 넘으면 열었을 때 이름 몇 개가 \
아니라 문서 목록을 마주하게 된다. 그런 폴더는 안을 다시 짜야 한다.

**검사 2 — 자기 더미가 그 아래 정리된 것 전부보다 큰 폴더가 있나?** 그 폴더의 하위들은 \
아무것도 걸러주지 못하고 있다는 뜻이다. 하위가 아예 없는 큰 폴더도 여기 해당한다.

**검사 3 — 루트에 남은 문서가 10건 이상인가?** 루트 더미는 읽는 사람이 트리 대신 읽어야 \
하는 목록이다.

**검사 4 — 최상위 폴더 중에, 다른 최상위 폴더의 하위 개념인 것이 있나?** 어떤 폴더의 설명이 \
다른 폴더 설명의 한 갈래를 말하고 있다면, 그 둘은 형제가 아니라 부모와 자식이다.

**검사 5 — 최상위 폴더가 20개를 넘나?** 깊이는 공짜가 아니지만 폭도 무한하지 않다. 다만 \
뜻이 분명한 이름 열두 개를 한 번에 훑는 것은 판단 한 번이고, 아무 뜻 없는 이름 하나가 뜻 \
있는 이름 열 개보다 나쁘다. 이름을 줄이려고 뜻을 뭉개지 마라.

**검사 6 — 지금 담고 있는 것과 설명이 안 맞는 폴더가 있나?** 폴더 설명은 그 폴더가 처음 \
생길 때 쓰인 문장이라, 그 뒤로 무엇이 쌓였는지 모른다. 설명이 틀리면 다음에 배치하는 쪽이 \
그 틀린 문장을 기준으로 판단하게 된다.

**검사 7 — 이름이 그 아래 있는 것을 말해주지 않는 폴더가 있나?** 폴더 이름도 처음 몇 건을 \
보고 지어진 것이라, 그 뒤에 다른 것들이 쌓이면 어긋난다. 특히 **문서 하나의 제목으로 지어진 \
이름 아래에 여러 갈래가 들어가 있으면** 그 이름은 안내가 아니라 방해다 -- 읽는 사람은 그 \
이름을 보고 자기가 찾는 것이 거기 있으리라 짐작하지 못한다. 자기 더미가 작아도 아래가 크면 \
해당한다.

**검사 8 — 부모 설명과 어긋나는 자식이 있나?** 부모 이름을 읽은 사람이 그 자식이 거기 \
있으리라고 예상하지 못한다면, 그 자식은 잘못 들어간 것이다. 한 번 잘못 들어간 폴더는 \
읽는 사람이 영영 못 찾는다 -- 트리에서 가장 비싼 오류다.

**검사 9 — 같은 것을 말하는 폴더가 두 곳에 따로 서 있나?** 이름이 겹치거나, 겹치지 않아도 \
설명을 읽으면 같은 갈래인 둘을 말한다. 읽는 사람은 둘 중 하나만 열어보고 나머지 절반을 \
영영 못 본다. 하나를 다른 하나 안으로 넣거나, 둘을 덮는 이름 아래로 함께 옮겨라.

검사에 걸리지 않은 것은 건드리지 마라. 물어볼 때마다 답이 달라지면 그 답들이 전부 \
임의적이라는 뜻이고, 같은 장서를 두 번 정리해서 같은 모양이 나오는 것이 이 트리를 믿을 수 \
있는 유일한 근거다.

답은 검사 결과 아홉 줄로 시작한다. 걸린 것이 없으면 `없음` 이라고 쓴다:

검사1: <25건을 넘는 폴더들, 각각 몇 건인지 / 또는 없음>
검사2: <자기 더미가 아래 전부보다 큰 폴더들 / 또는 없음>
검사3: <루트에 남은 문서 수>
검사4: <다른 최상위의 하위 개념인 최상위 폴더들 / 또는 없음>
검사5: <최상위 폴더 수>
검사6: <설명이 안 맞는 폴더들 / 또는 없음>
검사7: <이름이 아래 있는 것을 말해주지 않는 폴더들 / 또는 없음>
검사8: <부모와 어긋나는 자식 폴더들 / 또는 없음>
검사9: <같은 것을 말하면서 따로 서 있는 폴더 짝들 / 또는 없음>

아홉 검사가 모두 걸리지 않았으면(검사3은 10건 미만, 검사5는 20개 이하) 다음 한 줄만 더 \
쓰고 끝낸다:

KEEP

하나라도 걸렸으면 걸린 것마다 할 일을 쓴다. 검사 1·2·3 은 `REFILE`, 검사 4·5·7·8·9 는 \
`MOVE`, 검사 6 은 `SIGN` 이다:

MOVE: <지금 있는 경로> | <가야 할 곳>
REFILE: <안을 다시 짤 폴더 경로, 또는 루트를 가리키는 ROOT>
SIGN: <폴더 경로> | <지금 그 안에 있는 것을 말하는 한 문장>

`MOVE` 는 폴더를 통째로 옮긴다. 문서는 자기가 있는 폴더를 따라 움직인다. 세 가지로 쓸 수 \
있다:

* **다른 폴더 아래로** — `MOVE: 가/나 | 다/나` 는 `나` 를 통째로 `다` 밑으로 옮긴다. \
검사 4·8 이 걸린 자리에 쓴다.
* **이름 바꾸기** — `MOVE: 가/나 | 가/새이름` 처럼 **같은 자리에 다른 이름**을 주면 그 \
폴더의 이름이 바뀌고 안에 든 것은 그대로 따라온다. 검사 7 이 걸린 자리에 쓴다. 아래가 이미 \
잘 갈라져 있어도 이름이 틀렸으면 여기서만 고칠 수 있다.
* **루트로 끌어올리기** — `MOVE: 가/나 | 나`.

`REFILE` 은 그 폴더에 쌓인 문서를 그 폴더의 하위로 다시 꽂는다 -- 지목만 하면 되고, 하위 \
폴더를 무엇으로 할지는 그때 문서를 보고 정한다. **쌓인 문서가 없는 폴더에는 아무 일도 하지 \
않으니, 이름만 틀린 폴더에는 `REFILE` 이 아니라 `MOVE` 를 써라.** `REFILE: ROOT` 는 아직 \
어디에도 안 들어간 루트의 문서들을 다시 꽂는 것이고, 그것들은 이미 서 있는 폴더로 들어갈 \
수도 새 폴더가 될 수도 있다.\
"""


_REFILE = """\
너는 책장 하나의 안을 다시 짠다. 이 책장에는 문서가 하위 폴더 없이 그냥 쌓여 있고, 그래서 \
읽는 사람이 이 책장을 열면 폴더 이름 몇 개가 아니라 문서 목록을 마주한다. 그 목록을 폴더로 \
바꾸는 것이 네 일이다.

**이 책장 밖으로는 아무것도 내보내지 않는다.** 답은 이 책장 **안의** 하위 폴더이거나, \
그대로 두는 것뿐이다. 다른 책장으로 옮길 일이 있어 보여도 지금은 손대지 마라 -- 한 책장을 \
정리하다가 트리 전체를 흔들면, 고친 것보다 흐트러뜨린 것이 많아진다.

보여주는 형식은 이렇다. 이 책장에 이미 있는 하위 폴더는 한 줄씩:

  <이름>/  (<그 폴더에 직접 놓인 문서 수>) — <무엇을 담는다고 적혀 있는지>

다시 꽂을 문서도 한 줄씩:

  [D<번호>] <문서 자신의 제목> | <어떤 종류의 문서인지> | <무엇에 관한 것인지 몇 가지>

문서마다 이렇게 판단하라.

**1. 이미 있는 하위 폴더 중에 담는 곳이 있나?** 이름이 문서 제목과 닮았는지가 아니라, \
그 폴더가 담는다고 적힌 것과 이 문서가 다루는 것이 **겹치는지**를 물어라. 글자가 하나도 \
안 겹쳐도 주제가 그 안에 들어가면 그게 답이다.

**2. 없으면, 다른 문서들과 견줘봐라.** 둘 이상이 함께 속한다면 그게 새 하위 폴더다. 이름을 \
붙여라. 문서 하나만으로는 폴더를 만들지 않는다 -- 문서 한 건짜리 폴더는 읽는 사람에게 클릭 \
하나를 물리고 아무것도 걸러주지 않는다.

**3. 어디에도 안 맞으면 STAY.** 이 책장에 그대로 둔다. 정당한 답이다. 몇 건이 남는 것은 \
정상이고, 억지로 폴더를 만드는 것보다 낫다.

**전부를 하나의 하위 폴더에 넣지 마라.** 그건 나눈 것이 아니라 이 책장의 이름을 한 번 더 \
쓴 것이고, 읽는 사람은 클릭 한 번을 더 하고 같은 목록을 본다.

**한 하위 폴더에 25건이 넘게 몰린다면 그 갈래는 아직 덜 갈라진 것이다.** 그 안에서 더 \
가를 수 있는지 보라. 25건은 사람이 한 화면에서 훑을 수 있는 크기다.

**하위 폴더 이름은 그 문서들이 무엇에 관한 것인지를 말한다.** 문서가 무엇인지가 아니다 -- \
형식, 종류, 날짜, 발행 주체 같은 것은 거의 모든 문서에 해당해서 아무것도 가르지 못한다. \
이 책장 이름을 다른 말로 바꾼 것도 안 된다. 이 책장의 모든 문서가 이미 그 이름에 답하므로 \
아무것도 배제하지 못한다.

**폴더 이름이 그 안에 든 문서의 제목이면 그건 분류가 아니라 색인이다.** 문서 하나(와 그 \
문서에 딸린 것들)만 들어갈 수 있는 이름을 지으면 문서 수만큼 폴더가 생기고, 읽는 사람은 \
목록 하나를 목록 두 개로 바꿔 읽게 된다. 새로 짓는 이름은 **아직 오지 않은 문서도 들어올 \
수 있을 만큼** 넓어야 한다.

**"기타", "일반", "관련", "그 외" 같은 이름은 쓰지 않는다.**

**이 책장 이름이 틀렸으면 여기서 고쳐라.** 너는 지금 이 책장에 든 문서를 전부 보고 있고, \
이름이 안에 든 것을 말해주는지 판단할 수 있는 사람은 이 순간의 너뿐이다. 이름이 몇 건에만 \
맞고 나머지에는 안 맞는다면 -- 처음 몇 건으로 지어진 이름에 다른 것들이 쌓인 것이다 -- \
전부를 덮는 이름으로 바꿔라. 안 고치면 다음에 배치하는 쪽이 그 틀린 이름을 보고 또 엉뚱한 \
문서를 여기로 보낸다.

답은 문서당 한 줄이고, 그 외에는 아무것도 쓰지 마라:

D1: <하위 폴더 이름, 또는 STAY>
D2: <하위 폴더 이름, 또는 STAY>

새로 지은 하위 폴더는 무엇을 담는지 한 줄씩 덧붙여라:

SIGN: <하위 폴더 이름> | <한 문장>

이 책장 이름을 바꿔야 한다면 한 줄 더 쓴다. 바꿀 필요가 없으면 쓰지 마라:

이름: <이 책장의 새 이름>\
"""


@dataclass(frozen=True, slots=True)
class Folder:
    """One line of the tree as the model is shown it."""

    path: PurePosixPath
    note: str
    documents: int
    """Sitting directly in it, which is what says whether its children are doing any work."""

    held: int = 0
    """Everything below it as well.

    A class that has been divided holds nothing directly, and shown only its own count it
    reads as an empty folder: a reply looking for somewhere to put a document skipped the
    one folder in the tree that was its home, because the line said ``(0건)``.
    """

    children: int = 0


def _size(folder: Folder) -> str:
    """What a folder holds, said so that a divided one does not read as an empty one."""
    if folder.children:
        return f"({folder.held}건, 그중 여기 직접 {folder.documents}건, 하위 {folder.children}개)"
    return f"({folder.documents}건)"


def _tree(folders: list[Folder]) -> str:
    if not folders:
        return "  (아직 폴더가 없다. 전부 루트에 있다.)"
    lines = []
    for folder in sorted(folders, key=lambda f: str(f.path)):
        depth = len(folder.path.parts) - 1
        held = f" — {folder.note}" if folder.note else ""
        lines.append(f"  {'  ' * depth}{folder.path}/  {_size(folder)}{held}")
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


def build_refiling(
    *,
    folder: PurePosixPath,
    children: list[Folder],
    documents: list[tuple[str, str]],
    remaining: int,
    language: str = "",
) -> Prompt:
    """이 책장에 쌓인 문서를 이 책장의 하위로 다시 꽂는다.

    국소 재분류(docs/reclassification.md 8): 답의 범위가 이 폴더 안으로 한정된다. 트리 전체를
    보여주고 어디로든 보낼 수 있게 하면, 한 폴더를 고치는 일이 트리 전체를 흔드는 일이 된다.
    """
    inside = (
        "\n".join(
            f"  {child.path.name}/  {_size(child)}" + (f" — {child.note}" if child.note else "")
            for child in sorted(children, key=lambda c: str(c.path))
        )
        or "  (아직 하위 폴더가 없다)"
    )
    listed = "\n".join(f"  [{handle}] {line}" for handle, line in documents)
    say = (
        f"이 문서들은 `{language}` 로 쓰여 있다. 폴더 이름과 설명도 문서가 쓰는 말을 "
        f"그대로 써서 `{language}` 로 적어라.\n\n"
        if language
        else ""
    )
    # The root is a shelf too -- the one whose pile nothing else can reach.
    here = f"{folder}/" if folder.parts else "루트 (아직 어디에도 안 들어간 문서들)"
    return Prompt(
        system=_REFILE,
        user=(
            f"{say}지금 정리하는 책장: {here}\n\n"
            f"이 책장에 이미 있는 하위 폴더:\n{inside}\n\n"
            f"이 책장에 쌓여 있는 문서: {remaining}건\n\n"
            f"이번에 다시 꽂을 문서 ({len(documents)}건):\n{listed}"
        ),
    )


RENAME = "이 책장의 새 이름"
"""Key under which a refile's new name for its own shelf rides back with the signs.

A sentence, not a path: no folder is ever called this, so it cannot collide with the sign
of a real one, and it reads for what it is when a reply is dumped in a log."""


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
        elif tag == "이름":
            # A refile may rename the shelf it is dividing; filing never does, and a
            # stray 이름 line there resolves to a folder nobody asked for and is dropped.
            signs[RENAME] = value.strip().strip("/")
        elif tag.upper().startswith("D") and tag[1:].isdigit():
            placed[tag.upper()] = value
    return placed, signs


@dataclass(frozen=True, slots=True)
class Reviewed:
    """What the review asked for."""

    moves: tuple[tuple[str, str], ...] = ()
    refile: tuple[PurePosixPath, ...] = ()
    signs: Mapping[str, str] = field(default_factory=dict)

    @property
    def keep(self) -> bool:
        """Nothing asked for. Not a separate answer -- the same one said once."""
        return not self.moves and not self.refile


def parse_review(text: str) -> Reviewed:
    """What the review asked for, out of a reply that may also be prose."""
    moves: list[tuple[str, str]] = []
    refile: list[PurePosixPath] = []
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
        elif tag == "REFILE":
            named = value.partition("|")[0].strip().strip("/")
            if named.upper() in {"ROOT", "(ROOT)", "."}:
                refile.append(PurePosixPath())  # the pile nothing else can reach
            elif named:
                refile.append(PurePosixPath(named))
        elif tag == "SIGN":
            path, _, sentence = value.partition("|")
            if path.strip() and sentence.strip():
                signs[path.strip().strip("/")] = sentence.strip()
    return Reviewed(tuple(moves), tuple(dict.fromkeys(refile)), signs)
