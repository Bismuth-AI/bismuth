# 2026-08-13 `루트 문서 132개 재검토 필요` 실행 분석

## 결론

폴더가 일부만 만들어지고 132개가 루트에 남은 직접 원인은 API·인증·30개 창 격리 실패가
아니다. 195개 문서는 정확히 `30 × 6 + 15`의 일곱 창으로 분리됐고 재시도 문서는 섞이지 않았다.
첫 창만 22개를 이동했으며 뒤 여섯 창은 모두 이동 0건이었다. 뒤 창에서 Qwen이 일부 family
문서만 이동하는 계획을 반복 제출했고, 결정론적 검증기가 올바르게 이를 거절했지만 모델에게
family 구성과 수정 가능한 핸들을 충분히 알려 주지 못했다. 동시에 너무 작은 말단 선반을 즉시
재분할하고, 뒤 창의 새 증거가 들어와도 상위 경계를 다시 열지 않는 scope 선택 문제가 실패를
고착시켰다.

## 확인한 실제 실행

- run: `20260813T083201Z_c5f1773768`
- workflow: `batch:fca6a3839774`
- model: `qwen3.6-35b`
- sampling: temperature 0.2, top_p 0.8, presence_penalty 0, top_k 20,
  min_p 0, thinking disabled
- 입력 문서: 195
- 구조 창: 30, 30, 30, 30, 30, 30, 15
- artifact: 2,325 files, 약 29.7 MB
- structured calls: 470 calls, 합산 928.4초
- agent-chat calls: 218 calls, 합산 592.4초, output 113,512 tokens
- planner: 135 calls / 431.7초 / 84,183 output tokens
- 전체 실행: 약 27분, provider 호출 합산 대기만 약 25.3분

근거 파일은 `logs/runs/20260813T083201Z_c5f1773768/`의 `manifest.json`,
`timeline.jsonl`, `calls/`, `tools/`, `streams/`이다. 아래 판단은 UI 문구나 요약 로그만으로
추정하지 않고 이 artifact의 실제 request/response/tool result를 연결해 확인했다.

## 단계별 추적

### 1. 첫 창

`maintenance.window_started` 이벤트는 정확히 30개의 신규 document ID를 기록했다.
planner의 첫 호출 `llm_c6041395894c4aa4824147a5f2162381`은 빈 트리와 arrivals를 읽었다.
다음 응답 `llm_e34726d4c7bb4502a7dd246ebd98cbc0`은 실제 30개 카드에서 금융,
과학기술, 중소기업, 무역상업, 교육을 제안했다. `교육`에는 `D000005` 한 건만 배정되어
검증기가 `new shelf 교육 would contain only one document`로 거절했다. Qwen은 다음 호출에서
그 한 건을 루트에 남기는 방식으로 수정했다.

critic은 기술보증기금법, 과학관 관람규칙, 기업구조조정투자회사법 등의 강제 배치를 실제 카드와
원문으로 지적했다. progressive acceptance가 논쟁 없는 형제만 보존하여 루트에서 13개가
이동했다. 이어진 leaf pass는 직속 문서가 9개뿐인 `금융`을 즉시 다시 나눠 대통령령/시행규칙
선반을 만들고 9개를 이동했다. 첫 창 총 이동은 22개다.

### 2. 두 번째 이후 창

두 번째 창의 실제 request/response인 `llm_60546d…` 계열 호출에는 설정값과 30개 arrivals가
정상 전달됐고 Qwen은 tool-call finish로 계획을 제출했다. 하지만 이미 존재하는 선반의 문서와
현재 창 문서가 같은 법령 family인 경우 일부만 새 계획에 포함했다. 검증기는
`document family would be split across direct shelves`로 올바르게 거절했다.

문제는 당시 arrivals에 명시적인 family 그룹이 없었고, 거절 결과도 긴 법령 제목만 보여 주며
`D000…` 핸들별 현재 선반과 최종 선반을 주지 않았다는 점이다. Qwen은 무엇을 함께 옮겨야 하는지
정확히 복구하지 못하고 재탐색·재제출을 반복했다. 같은 패턴으로 창 2~7의 이동은 모두 0건이었다.

### 3. 불가능한 leaf scope

마지막 창에서는 `금융/대통령령`처럼 문서 두 건뿐인 leaf가 검토 대상으로 선택됐다. 그 안에는
법률 문서가 잘못 들어가 있었고 Qwen은 실제로 부모 `금융`에서 고쳐야 한다고 판단했다. 그러나
에이전트에 부여된 scope는 `금융/대통령령`이어서 부모에 형제를 추가하거나 기존 금융 문서와 함께
family를 교정하는 계획을 검증기가 받아들일 수 없었다. 모델의 의미 판단은 맞았지만 도구 권한
계약이 그 수정을 표현할 수 없었다.

### 4. scope가 다시 열리지 않은 이유

한 drain 동안 `reviewed_scopes`를 고정 집합으로 누적했다. 첫 창에서 `금융`을 검토한 뒤 다음 창이
그 아래에 새 문서를 추가해 fingerprint가 달라져도 `금융`은 다시 후보가 되지 않았다. 선택기의
leaf 세분화 최소값도 4건이라 작은 선반이 너무 빨리 새로운 경계가 됐다. 그 결과 상위 정책 분야
경계를 보정할 기회를 잃고 법령 유형 말단으로 내려갔다.

## 반영한 수정

1. arrivals에 `FAMILY=F…`와 `FAMILY_MEMBERS=D…`를 표시한다.
2. family 검증 오류에 각 이동 가능 핸들의 `current`와 `final` 선반을 포함한다.
3. `add_sibling`이 현재 창의 AI 관리 기존 자식 문서와 신규 문서를 한 계획에서 교정할 수 있게 한다.
   사람 관리 조상을 통과하거나 기존 경계값을 비우는 계획은 거절한다.
4. 평평한 선반은 직속 문서 30건 전에는 자동 세분하지 않는다.
5. 관리 경계 아래에 새 문서가 들어오면 leaf가 아니라 기존 경계의 부모를 다시 검토한다.
6. 검토 scope를 고정 집합이 아닌 매 창의 fingerprint로 다시 계산한다.
7. Qwen agent의 planner/critic turn과 conclusion turn을 줄이고 동일 tool call 반복을 1회에서 막는다.
8. batch의 추출·카드 준비는 설정된 LLM 동시성으로 병렬화하되, 실제 filing·트리 변경·30건
   checkpoint는 순서대로 직렬 실행한다.

## 재현 및 검증 원칙

단위 테스트는 family 표시, 정확한 rejection 피드백, 기존 자식에서의 atomic repair, 작은 leaf의
세분화 억제, 변경된 상위 경계 재개방을 검증한다. 실제 모델 테스트는 사용자 볼트를 읽거나
변경하지 않고 production system prompt와 tool schema를 사용한 합성 경계에서 수행한다. 성공
조건은 이미 올바른 문서는 no-op 이동에서 제외하고, 갈라진 family는 한 직속 선반으로 모으며,
새 family 구성원은 같은 새 형제로 함께 보내는 단 한 번의 유효 `submit_plan`이다.

최종 검증 결과:

- Ruff: 통과
- mypy: 변경 source 7개 통과
- 현재 API/organizer/ingest/logging 관련 회귀 테스트: 102 passed
- 실제 Qwen production-contract check: `conclusion`, 5 turns, 유효 제출 1회,
  rejection 0회
- Qwen 계획: `D000001` no-op 제외, `D000002 → 금융`,
  `D000003,D000004 → 연구`

전체 저장소 테스트는 420 passed, 1 skipped, 39 failed였다. 이 중 38건은 현재 API에서 제거한
구형 per-document subdivision이 네 문서부터 자동 호출된다는 과거 계약을 여전히 전제하는 suite이고,
1건은 이번 변경 전부터 `Emerging.note` 필드를 기대하는 구형 schema-order 테스트다. 현재 bounded
organizer 경로의 102개 회귀 테스트와 실제 provider 계약 테스트는 모두 통과했다. 구형 suite를
새 동작에 맞춰 대량 수정하면 제거한 기능을 다시 제품 계약처럼 보이게 하므로 이번 수정 범위에서는
그 사실을 분리 기록한다.
