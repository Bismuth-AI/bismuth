# 앞서 있었던 것

이 문제는 새것이 아니다. 증분으로 만들어지는 분류 구조를 문서가 계속 들어오는 동안 유지하는
일은 1987년부터 연구돼 있고, 도서관은 백 년째 하고 있으며, 지금 이 순간에도 미해결로 남은
조각이 있다. 여기 있는 것은 **우리가 읽고 설계를 바꾼 것**만이다.

이 문서는 배경이다. 그래서 무엇을 하기로 했는지는
[ADR-0018](adr/0018-maintenance-is-four-operators.md)에 있다.

## Cobweb — 네 연산자와 역연산

Fisher(1987)의 증분 개념 형성. 인스턴스가 하나 들어올 때마다 경로 위 각 노드에서 **네 연산을
모두 시뮬레이션하고 점수(category utility)가 가장 높은 것을 고른다.**

| 연산 | 하는 일 |
| --- | --- |
| insert | 가장 맞는 자식에 넣는다 |
| create | 새 자식을 만든다 |
| merge | 가장 맞는 두 자식을 하나로 합친다 |
| split | 가장 맞는 자식을 지우고 그 자식들을 이 층으로 올린다 |

핵심은 문헌의 이 문장이다: **merge와 split은 역연산이며, 이것이 Cobweb이 앞선 차례에 저지른
실수를 교정하게 해준다.**

우리에게는 insert(배치), create(세분화), 좁은 형태의 merge(묶어 올리기)가 있었고 **split이
없었다.** 그래서 한 번 그은 층은 영구적이었다.

Cobweb이 매 삽입마다 네 연산을 다 재볼 수 있는 이유는 **점수가 산술**이기 때문이다 — 속성
분포 위의 계산. 우리의 같은 판단은 모델 호출이라 그 빈도로는 못 한다. 그래서 우리 쪽 설계는
**코드가 값싼 신호로 어디를 물을지 고르고 모델은 판단만 한다**로 갈린다.

- [Cobweb: An Incremental and Hierarchical Model of Human-Like Category Learning](https://arxiv.org/pdf/2403.03835)
- [Incremental Hierarchical Clustering of Text Documents (Sahoo, CIKM'06)](https://www.cs.cmu.edu/~callan/Papers/cikm06-nsahoo.pdf)

## TnT-LLM — 표본으로 설계하고 전수로 분류한다

LLM으로 분류체계를 만드는 파이프라인. **표본 문서를 요약 → 분류체계를 생성·갱신·정제 반복 →
그 다음에 전수 분류.** 시드 라벨이 필요 없고, 분류체계의 품질만이 아니라 **그 뒤 분류에
쓸모가 있는지**까지 함께 본다.

우리가 실측으로 도달한 것과 같은 모양이다 — 문서 75건이면 300건과 같은 축과 같은 상위 이름
넷이 나왔다(9회 반복, `docs/eval/redesign-lab.md`). 재설계의 설계 단계가 O(1)일 수 있는 근거다.

- [TnT-LLM: Text Mining at Scale with Large Language Models](https://dl.acm.org/doi/pdf/10.1145/3637528.3671647)

## TaxoAdapt · EvoTaxo · GraphRAG — 무엇이 변했는지 아는 문제

움직이는 코퍼스에 LLM 분류체계를 맞추는 최근 작업들. 공통점은 **expand·split·merge·relabel을
국소적으로 적용하고 전면 재구축을 피한다**는 것.

그리고 Microsoft가 GraphRAG에서 같은 조각을 **미해결 설계 과제로 명시**한다 — 커뮤니티가
"drift"했다고 볼 만큼 변했는지 판단해서 바뀐 것만 다시 계산하는 문제. 우리만 헤매는 게 아니다.

drift 연구 쪽에서는 **클러스터 지름의 변화로 split과 merge를 발동**시킨다. 안정된 구조에서는
구조를 세분화할수록 클러스터 안의 퍼짐이 줄어든다는 관측에 기댄다. 우리 쪽 대응물은 폴더가
든 카드 어휘의 퍼짐이고, 계산이지 판단이 아니므로 코퍼스 중립이다.

- [TaxoAdapt](https://arxiv.org/pdf/2506.10737) · [EvoTaxo](https://arxiv.org/pdf/2603.19711)
- [GraphRAG: incremental indexing](https://github.com/microsoft/graphrag/issues/741)

## 순서 의존성 — 완화하는 것이지 없애는 것이 아니다

증분 클러스터링에서 제시 순서가 결과를 바꾸는 것은 **널리 알려진 성질**이고, 어떤 순서로도
특정 구조에 도달할 수 없는 경우가 존재한다는 결과까지 있다. 표준 완화책은 **전후처리**다.

우리는 이것을 직접 측정했다 — `temperature=0`에서 같은 코드·같은 문서·같은 순서로 두 번
돌려도 다른 트리가 나온다. 그러므로 전체 트리를 다시 보는 패스는 **증분 경로가 실패했다는
증거가 아니라 이 부류의 알고리즘이 원래 쓰는 후처리**다.

- [Order preserving hierarchical agglomerative clustering](https://link.springer.com/article/10.1007/s10994-021-06125-0)
- [Incremental cluster validity index-guided online learning](https://arxiv.org/pdf/2108.07743)

## 도서관 — 전면 재분류의 실제 비용

1960~70년대 미국 대학도서관들이 듀이에서 LC로 대규모 재분류를 시도했다. 예산이 끊기면서
프로젝트가 중단됐고, **한 장서 안에 두 체계가 남았다.**

우리 설계에 그대로 걸린다: 재설계는 **전부 아니면 전무**여야 한다. 중간에 멈춘 재설계는
쪼개진 장서를 남긴다.

- [Reclassification in Academic Research Libraries](https://www.tandfonline.com/doi/abs/10.1080/01639374.2011.532406)

## 랑가나단 — 단일 계층은 개정을 요구한다

패싯 분류가 왜 생겼는지에 대한 그의 진단: 계층 분류는 새 지식을 넣을 때마다 질서를 잃고,
그 질서를 잃는 것은 곧 **완전 개정**을 요구한다는 것.

**주기적 개정은 우리가 없앨 수 있는 결함이 아니라 우리가 고른 자료구조의 성질이다.** 파일
시스템이 제품인 이상(ADR-0001) 단일 계층이고, 그러면 개정은 남는다. 목표는 "개정이 필요 없는
구조"가 아니라 **"개정이 감당 가능한 구조"**다.

**결정: 패싯은 비목표다**(SPEC §5). 개정을 없애는 쪽이 아니라 감당 가능하게 만드는 쪽을
골랐고, 둘은 같은 전제를 두고 다투므로 동시에 가질 수 없다. 읽는 쪽이 에이전트라는 것이
그 선택을 싸게 만든다 — 아래 마지막 절이 그 이유다.

- [Faceted Classification — The Discipline of Organizing](https://berkeley.pressbooks.pub/tdo4p/chapter/faceted-classification/)
- [Ranganathan and the faceted classification theory](https://www.redalyc.org/journal/3843/384357586006/html/)

## 긴 문서를 카드 한 장으로 — 우리 방식에는 이름이 있다

문서를 조각으로 나눠 순서대로 읽으며 카드를 고쳐 쓰는 것은 **incremental updating**이라고
불리는 알려진 워크플로다. 대안은 **hierarchical merging** — 조각을 따로 요약하고 그 요약들을
재귀적으로 병합하는 것.

[BooookScore](https://arxiv.org/abs/2310.00785)(ICLR 2024)가 둘을 체계적으로 비교했다.

| | 일관성 | 세부 |
|---|---|---|
| 계층 병합 | 높음 (GPT-4 90.8) | 적음 |
| **증분 갱신** | 낮음 (82.4) | **많음** |

**우리는 세부 쪽이 맞다.** 카드는 사람이 읽을 요약이 아니라 **분류가 읽는 표면**이기 때문이다.
일관성이 떨어진 카드는 읽기 불편할 뿐이지만, 빠진 주제는 축이 되지 못하고 축이 좁으면 폴더가
안 나뉜다 — 여러 판에서 반복해서 본 그 병이다.

그리고 주석자가 찾은 여덟 가지 오류 중 **가장 흔한 것이 개체 누락과 사건 누락**이었다. 우리
실패도 정확히 그 형태다.

**조각 크기가 가장 큰 레버다.** 같은 논문이 여러 파라미터 중 chunk size의 영향이 가장 크다고
보고한다 — Claude 2가 작은 조각에서 78.6, 88K 토큰 조각에서 90.9. 우리는 12,000자 창을 쓰고
있고(실측 카드 프롬프트 최대 10,739토큰), 창을 키우면 갱신 횟수·누락·반복 출력 사고가 동시에
줄어든다. 300건 중 131건은 이미 한 조각이다.

- [BooookScore (arXiv:2310.00785)](https://arxiv.org/abs/2310.00785)

### 계층 병합을 안 고르는 이유

원형은 [Recursively Summarizing Books with Human Feedback](https://arxiv.org/abs/2109.10862)
(OpenAI, 2021)이고, 재귀 분해로 사람이 책을 다 안 읽고도 평가할 수 있게 만든 것이 요점이었다.

그런데 [Context-Aware Hierarchical Merging](https://arxiv.org/abs/2502.00977)(ACL Findings
2025)이 **재귀 병합은 환각을 증폭시킨다**고 지적한다. 중간 요약을 다시 요약하므로 틀린 것이
굳는다. 그 논문의 해법은 병합 단계에 원문 근거를 다시 넣는 것이다.

우리에게 이건 치명적이다. **카드가 틀리면 그 문서는 영원히 잘못 분류된다** — 배치도 세분화도
재설계도 원문을 다시 읽지 않고 카드만 보기 때문이다(SPEC 3.1).

### 순서대로 읽는 것의 비용

[On Positional Bias of Faithfulness for Long-form Summarization](https://aclanthology.org/2025.naacl-long.442.pdf)
(NAACL 2025): 생성된 요약의 충실도가 **U자 곡선과 lead bias**를 보인다. 앞쪽을 더 충실하게
요약한다.

증분 갱신은 순서대로 읽으므로 여기 정면으로 노출된다. 우리 `summary`는 조각마다 다시 쓰이고,
그때 살아남는 쪽이 앞이라는 뜻이다. **아직 재보지 않았다.**

16조각을 넘으면 앞에서부터가 아니라 처음~끝을 균등 간격으로 뽑는 우리 규칙은 이 편향에 대한
올바른 방향이다.

### `_densify`에도 이름이 있다 — Chain of Density

[From Sparse to Dense](https://aclanthology.org/2023.newsum-1.7/)(Adams et al., 2023): 희소한
요약에서 시작해 **길이를 늘리지 않으면서** 빠진 핵심 개체를 1~3개씩 반복해서 편입한다. 자리를
만들려고 기존 내용을 압축한다.

우리 카드 프롬프트가 이미 그 규칙을 담고 있다 — *"3~4문장으로 유지하고, 이번 부분이 기존보다
중요하면 약한 것을 버려 자리를 만들라."* 다른 점은 CoD가 **빠진 개체를 명시적으로 찾는 단계**를
두는 데 있고, 우리에게는 그 단계가 없다.

## 읽는 쪽이 에이전트라는 것의 의미

LLM-Wiki 계열 작업의 관측: 트리를 순회하는 에이전트는 **페이지를 열어보고 나서 쓸모를
판단**하므로, 한 번에 맞혀야 하는 검색과 달리 **불완전한 구조에서 회복한다.**

그래서 목표는 옳은 트리가 아니라 **탐색 가능하고 정직한** 트리다. 이름이 무엇을 배제하는지
말해주기만 하면, 그 이름이 최선이 아니어도 읽는 쪽이 복구한다.

태그 쪽 관측도 같은 방향이다 — 태그는 **설명이 붙은 공용 목록으로 유지하고 새로 만드는 데
인색할 때** 가장 잘 듣는다. 우리 폴더 노트가 그 목록이다.

- [Retrieval as Reasoning: Self-Evolving Agent-Native Retrieval via LLM-Wiki](https://arxiv.org/pdf/2605.25480)
