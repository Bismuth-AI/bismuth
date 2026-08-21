# 범주가 감당이 안 될 때 — 재분류의 선행 연구

> 상황: 도서관을 막 차렸다. 책이 몇 권 없을 때 만든 범주가 있다. 책이 늘면서 그 범주로는
> 감당이 안 된다. 범주를 고쳐야 하고, **이미 청구기호를 받아 꽂힌 책들도 다시 꽂아야 한다.**

이 문서는 그 상황에 이미 이름과 방법론이 있다는 것, 그리고 그중 무엇이 우리에게 적용되고
무엇이 적용되지 않는지를 정리한다. 조사일 2026-08-21.

---

## 1. 문헌적 보증 (Literary Warrant) — 범주는 언제 생기나

**Hulme (1911).** 분류 범주는 철학적·과학적 체계에서 연역하는 것이 아니라 **실제로 장서에
들어와 있는 문헌**에서 나와야 한다.

> 새 주제는 그 존재를 정당화할 만큼 충분한 양의 출판 문헌이 있을 때에만 분류표·주제명표·
> 색인 어휘에 추가된다.

특정 장서로부터 분류를 세우는 이 원칙은, 분류가 보편적이어야 하고 모든 지식을 다뤄야 한다는
철학적 입장과 대립하는 자리에 놓인다.

**우리 코드에 이미 있다.** 배치 프롬프트의 *"둘 이상이 함께 속할 때만 폴더를 만들어라.
문서 하나는 부류가 아니다"* 가 문헌적 보증의 구현이다. 이름을 몰랐을 뿐이다.

- [Literary warrant — ISKO Encyclopedia](https://www.isko.org/cyclo/literary_warrant)
- [Hulme's Concept of Literary Warrant](https://www.researchgate.net/publication/261654775_Hulme's_Concept_of_Literary_Warrant)

## 2. 넓은 분류 / 좁은 분류 — 장서 크기가 깊이를 정한다

| | |
|---|---|
| **좁은 분류** (close classification) | 표기법이 허용하는 최대한 세밀하게 분류 |
| **넓은 분류** (broad classification) | 더 구체적인 번호가 있어도 논리적으로 줄여 큰 범주로 |

> 좁은 분류는 관련 주제를 많이 소장한 **큰 도서관**에 필요하고, 넓은 분류는 분야당 책이 적은
> **일반 도서관**에 적합하다. DDC 축약판은 2만 권 이하 장서를 위한 것이다.

**함의:** 책이 적을 때 `금융` 하나로 두는 것은 틀린 게 아니라 그 규모에 맞는 답이다. 장서가
자라면 깊이도 자라는 것이 정상이다. 깊이를 고정 규칙으로 정할 게 아니다.

- [Library Classification — ScienceDirect](https://www.sciencedirect.com/topics/social-sciences/library-classification)
- [DDC glossary — OCLC](https://help.oclc.org/Librarian_Toolbox/OCLC_glossaries/Dewey_Decimal_Classification_glossary)

## 3. 접대성 (Hospitality) — Ranganathan

> 접대성이란 **기존의 것을 흐트러뜨리지 않으면서** 새 주제를 제자리에 받아들이는 능력이다.

Ranganathan은 이를 위해 표기법에 빈자리를 만드는 기법(sector notation, emptying digits)을
발명했다. 배열 안에 빈 자리가 없을 때 새 주제를 끼워 넣기 위한 장치다.

**우리는 표기법이 없어 그 장치를 쓸 수 없다.** 그러나 "기존을 흐트러뜨리지 않는다"는 요구
자체는 남는다 — 다시 꽂기가 비싼 이유가 이것이다. **§6에서 이 비용이 우리에게 얼마나
적용되는지 따진다.**

- [Ranganathan's Prolegomena to Library Classification](https://www.miskatonic.org/library/prolegomena.html)
- [Notation: hospitality — INFLIBNET](https://ebooks.inflibnet.ac.in/lisp2/chapter/notation-kinds-qualities-mnemonics-and-hospitality/)

## 4. Phoenix Schedule — 못 버티면 통째로 다시 짠다

DDC의 **완전 개정**(complete revision, 옛 이름 phoenix schedule):

> 기준 번호는 이전 판과 같게 두되 **하위 구분을 사실상 전부 바꾼다.**

실제 사례: 무기화학·유기화학(546, 547)이 최초의 phoenix. 17판에서 심리학, 18판에서 법학과
수학이 통째로 재주조됐다.

**그 대가에 이름이 있다 — split collection:**

> **쪼개진 장서**(옛 번호로 꽂힌 책과 새 번호로 꽂힌 책이 섞이는 것)를 피하려면, 기존
> 소장본을 전부 찾아내 새 번호를 다시 매겨야 한다.

즉 **"반쯤 하다 만 재분류가 제일 나쁘다"** 는 우리 직관이 이 분야의 공식 용어다. 이것이
재분류를 원자적으로 만들어야 하는 이유이며, 선택이 아니라 요구사항이다.

- [Curwen, *Revision of classification schemes: policies and practices* (1978)](https://www.blissclassification.org.uk/Curwen1978.pdf)
- [DDC glossary — complete revision](https://help.oclc.org/Librarian_Toolbox/OCLC_glossaries/Dewey_Decimal_Classification_glossary)

## 5. 실제 재분류 비용 (물리 도서관)

| 사례 | 방식 | 규모 | 기간 |
|---|---|---|---|
| Joint University Libraries (Vanderbilt 외) | 수작업 | 260,703권 | 6년 |
| Western Kentucky University | 자동화 | 390,000권 | 2년 |

계획 시 고려사항으로 비용·팀 구성·기간과 함께 **"서비스 중단과 이용자가 책 위치를 새로
익혀야 하는 혼란"** 이 명시된다.

- [ERIC ED104379 — 수작업 vs 자동화 재분류 비교](https://eric.ed.gov/?id=ED104379)

## 6. 그 비용 중 우리에게 남는 것과 사라지는 것

| 물리 도서관의 비용 | 우리 |
|---|---|
| 책등 라벨·청구기호 재부착 | **없음** |
| **이용자가 서가 위치를 새로 익힘** | **없음** — 읽는 쪽이 에이전트고 매번 `ls` 를 다시 한다 |
| 6년 / 26만권 | 폴더 하나당 LLM 콜 10회 안팎 |
| **split collection** | **남는다** — 원자성으로 막아야 한다 |
| 밖에서 경로를 가리키는 링크 | 지금은 없음. 생기면 비용이 된다 |

접대성이 지키려던 핵심 — *이용자가 익힌 자리* — 이 우리에게는 아예 없다. **에이전트는 근육
기억이 없다.**

## 7. 그래서 "초기 축을 신뢰한다"는 뜻이 아니다

문헌들이 기존 구조를 지키려는 이유는 **"초기 범주가 맞을 가능성이 높아서"가 아니라 "바꾸는
것이 비싸서"** 다. 셋은 명확히 구분된다:

- **문헌적 보증**은 오히려 초기 축의 근거가 약하다고 말한다. 13권으로 만든 범주는 13권만큼만
  보증된다.
- **접대성**의 근거는 인식론이 아니라 물리적·인지적 비용이다.
- **Phoenix schedule의 존재 자체가 반증이다.** 그 분야가 "초기 축이 충분히 틀릴 수 있다"를
  공식 절차로 인정했다.
- 「가지 안에서만 재분류」도 신뢰도 논변이 아니라 **계산 비용** 논변이다.

**우리 실측도 초기 축이 덜 믿을 만하다고 말한다:**

- 코드에 남은 기록: *format 축을 고른 두 판 모두 **문서 다섯 건**에서 축을 고정했고, subject
  축을 고른 판은 전부 **열다섯 건 이상**에서 고정했다.*
- 2026-08-20 실측: `금융` 폴더가 문서 13건으로 태어나 **20분 24초 동안 축 제안 15회를 전부
  거절당한 뒤에야** `소속 법률의 명칭` 으로 정착했다.

## 8. 재분류의 범위 — 가지 안인가, 전체인가

taxonomy 문헌의 처방:

> 어떤 범주에 하위 범주가 정의되면, **그 가지에 배정된 모든 문서를 새로 만들어진 하위
> 범주만으로 다시 분류한다.** 이 국소적 재배정이 무관한 범주의 간섭 없이 분류를 깊게 만든다.

같은 계열에서 비용 경고도 함께 나온다:

> 하향식 분류 구축에서 지배적인 계산 비용은 **재분류**에서 나온다. 각 문서가 모든 층에서
> 다시 배정되어야 하기 때문이다.

**우리 실측이 이 처방을 지지한다.** 재배치 대상 폴더의 문서 중 다른 가지로 갈 후보:

| 폴더 | 문서 | 다른 루트 폴더로 갈 후보 | 법률 가족(2건 이상) |
|---|---|---|---|
| `금융` | 95건 | **1건** | 37개 → 91건 |
| `과학기술` | 81건 | **0건** | 25개 → 65건 |

1건 때문에 94건을 전체 트리에 대고 다시 묻는 것은, 한 가지의 문제를 트리 전체로 번지게 한다.
**국소 재분류가 맞다.**

- [TaxoAdapt (ACL 2025)](https://aclanthology.org/2025.acl-long.1442/) — 코퍼스 주제 분포에
  따라 폭과 깊이를 함께 확장. 부모에 이미 분류된 문서의 처리는 초록만으로 확인되지 않음
- [Taxonomy system for enterprise data management (US 8577823)](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/8577823)

## 9. 이 조사에서 나온 설계 결론

| 우리가 부르던 이름 | 제대로 된 이름 | 결론 |
|---|---|---|
| "둘 이상 모이면 폴더" | 문헌적 보증 | 이미 맞게 하고 있다 |
| "금융 95건은 나눠야" | 좁은 분류로의 이행 | 장서가 자라면 깊이도 자라는 게 정상 |
| "다시 꽂기가 비싸다" | 접대성 | **우리에겐 대부분 해당 없음** |
| "반쯤 하다 말면 최악" | split collection | **원자성은 요구사항** |
| "폴더 다시 나누기" | complete revision (phoenix) | 한 가지를 통째로 다시 짜고 그 아래를 재배정 |
| "트리 전체를 보여주자" | — | **철회.** 국소 재분류가 맞다 (§8) |

따라서 우리 비용 구조에서 맞는 규칙은 **"바꾸기를 주저하라"가 아니라 "바꿀 거면 끝까지
바꿔라"** 이다.

### 다만 하나는 남는다 — 순서 독립성

매번 다시 그리면 같은 장서를 다른 순서로 두 번 넣었을 때 다른 트리가 나온다(SPEC.md §6.2
「순서 독립성」). 이것은 *읽는 사람의 불편*이 아니라 **시스템의 답이 임의적이라는 신호**다.

그러므로 재분류를 억제하는 문장은 프롬프트에 남되, 근거가 바뀐다:

- ~~"읽는 사람이 익힌 자리가 사라지니까"~~ ← 물리 도서관의 근거, 우리에겐 해당 없음
- **"매번 답이 달라지면 그 답들이 다 임의적이라는 뜻이니까"** ← 우리에게 맞는 근거

---

## 10. 이 브랜치에 들어간 구현 (`feat/simple-batch`)

점검이 할 수 있는 말이 둘이 됐다. 하나는 원래 있던 `MOVE`(폴더째 옮기기)이고, 새로 생긴 것이
`REFILE`(폴더 하나의 안을 다시 짜기)이다.

```
MOVE: 금융 | 경제/금융      ← 폴더가 통째로 이사한다
REFILE: 경제/금융           ← 그 폴더에 쌓인 문서가 그 폴더의 하위로 다시 꽂힌다
```

| 설계 결론 | 구현 |
|---|---|
| 국소 재분류 (§8) | `build_refiling` 은 트리 전체가 아니라 **그 폴더와 그 하위만** 보여준다. 답은 하위 폴더 이름 아니면 `STAY` 뿐이라 문서가 폴더 밖으로 나갈 길이 없다 |
| 원자성 (§4, split collection) | 문서가 몇 건이든 열 건씩 나눠 묻지만, 저널 항목은 **폴더당 하나**다. 중간에 실패하면 트랜잭터가 통째로 되돌려서 옛 자리와 새 자리에 반씩 걸친 장서가 생기지 않는다 |
| 순서 독립성 (§9) | 점검 프롬프트의 자제 근거를 "읽는 사람이 익힌 자리" 에서 **"매번 답이 달라지면 그 답들이 다 임의적"** 으로 바꿨다 |
| 이행이지 되돌리기가 아니다 (§3) | 앞 배치가 지은 하위 폴더를 다음 배치에 그대로 보여준다. 같은 것을 조금씩 다른 이름으로 두 번 짓지 않게 |

코드가 거절하는 것은 둘뿐이고, 둘 다 도메인 지식이 아니라 **모양**에 대한 사실이다
(SPEC.md §2):

- 하나의 하위 폴더가 더미 전체를 가져가면 거절한다 — 그건 나눈 것이 아니라 그 폴더 이름을
  한 번 더 쓴 것이고, 읽는 사람은 클릭 하나를 더 하고 같은 목록을 본다.
- 문서 한 건짜리로 끝난 **새** 폴더는 만들지 않는다 — 아무것도 걸러주지 못하면서 클릭만 든다.

나머지 판단(무엇으로 나눌지, 이름을 뭐라 할지, 몇 건이 그냥 남을지)은 전부 프롬프트에 있다.

---

## 참고문헌

- Hulme, E. W. (1911). *Principles of Book Classification*. — 문헌적 보증
- Ranganathan, S. R. *Prolegomena to Library Classification*. — 접대성, 배열/연쇄
- Curwen, A. G. (1978). [*Revision of classification schemes: policies and practices*](https://www.blissclassification.org.uk/Curwen1978.pdf) — phoenix schedule, split collection
- [ERIC ED104379](https://eric.ed.gov/?id=ED104379) — 수작업/자동화 재분류 실측 비교
- [ISKO Encyclopedia: Literary warrant](https://www.isko.org/cyclo/literary_warrant)
- [OCLC: DDC glossary](https://help.oclc.org/Librarian_Toolbox/OCLC_glossaries/Dewey_Decimal_Classification_glossary) — complete revision, close/broad classification
- Kargupta, P. et al. (2025). [*TaxoAdapt: Aligning LLM-Based Multidimensional Taxonomy Construction to Evolving Research Corpora*](https://aclanthology.org/2025.acl-long.1442/). ACL.
- Fisher, D. (1987). *Knowledge acquisition via incremental conceptual clustering* (Cobweb) — insert/create/merge/split. 이 저장소의 ADR-0018이 이미 인용.
