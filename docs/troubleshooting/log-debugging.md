# 실제 LLM 실행 로그로 디버깅하기

이 문서는 최종 UI 문구나 결과 트리만 보고 원인을 추측하지 않고, 한 실행의 실제 입력과 출력을
단계별로 재구성하는 절차다. 원칙은 [`SPEC.md` §6.3](../../SPEC.md#63-디버깅과-개선은-실제-실행-증거에서-시작한다)이
정하고, 로그 구조의 근거는 [ADR-0015](../adr/0015-run-scoped-diagnostics.md)에 있다.

## 1. 로그 구조

`logs/` 최상위 세 파일은 현재 실행의 compact view다.

- `bismuth.log`: 사람이 읽는 서버 시간표와 예외
- `trace.jsonl`: 현재 실행의 정규화된 timeline
- `llm.jsonl`: 한 줄에 한 LLM 호출인 작은 artifact 인덱스

재시작해도 보존되는 정확한 증거는 `logs/runs/<run_id>/`에 있다.

```text
manifest.json                  실행·버전·모델·생성 설정
timeline.jsonl                 단계별 이벤트와 모든 조인 ID
llm.jsonl                      compact 호출 인덱스
calls/<call_id>.request.json   provider 직전 실제 입력
calls/<call_id>.response.json  재조립된 실제 출력·usage·종료 사유
streams/<call_id>.jsonl.gz     전체 provider raw chunk
tools/<artifact_id>.json       도구가 반환한 전체 원문
bismuth.log                    해당 실행의 텍스트 로그
```

`logs/latest.json`은 가장 최근 run 디렉터리를 가리킨다. 운영 로그와 `llm-guard-experiment.*`,
`codex-server.*` 같은 과거 실험·외부 로그를 섞어 분석하지 않는다.

## 2. 가장 빠른 조사 순서

### 2.1 실행 전체 요약

```powershell
$env:PYTHONUTF8='1'
.\.venv\Scripts\python.exe scripts\where_the_time_went.py logs
.\.venv\Scripts\python.exe scripts\inspect_run.py logs
```

첫 명령은 문서 처리 단계별 시간과 structured·agent 호출을 모두 합산한다. 두 번째 명령은
`run_id`, stage와 window 목록, event 개수를 보여준다. raw stream은 아직 열지 않는다.

### 2.2 한 문서 또는 한 단계를 특정

```powershell
.\.venv\Scripts\python.exe scripts\inspect_run.py logs --document "<document_id>"

.\.venv\Scripts\python.exe scripts\inspect_run.py logs --stage subdivision.review
.\.venv\Scripts\python.exe scripts\inspect_run.py logs --event subdivide.rejected
```

`document_id`가 이 저장소의 1차 조인 키다. 한 문서로 필터하면 `document.read` → `card.*` →
`place.decided` → `document.filed` → `subdivide.*`가 한 줄기로 나온다.

단계는 `stage`로 좁힌다. 현재 파이프라인이 쓰는 값:

| stage | window_id | 무엇을 묻는 단계인가 |
|---|---|---|
| `card.first` / `card.update` / `card.densify` | `<document_id>:window-NNN` | 문서 한 window의 카드 생성·갱신 |
| `placement` | `placement:NN` | 트리 한 층에서의 폴더 선택 |
| `subdivision.emerging` / `subdivision.members` | — | 자란 부류 하나와 그 구성원 |
| `subdivision.review` | `review:signs-NNN` / `review:docs-NNN` | 현재 경계가 유지되는지의 packet별 boolean |
| `subdivision.replacement` | `sketch:*` | 경계 교체 sketch와 reduce |
| `subdivision.routing` / `.audit` / `.audit.current` | — | 기존 표지 재사용과 의미 감사 |

**packet으로 나뉘는 단계는 packet별 답을 먼저 본다.** review boolean은 fail-closed로 병합되므로
합쳐진 결과만 보면 어느 packet이 왜 실패했는지 알 수 없다. `--window`로 그 packet만 꺼낸다.

`subdivide.skipped`는 "묻지 않았다"이고 `subdivide.rejected`는 "묻고 거절했다"이다. 둘을 같은
침묵으로 읽지 않는다. `reason` 필드가 어느 쪽인지 말한다.

### 2.3 실제 LLM 입력과 출력 확인

timeline의 `llm.call` 또는 `agent.turn`에서 `call_id`를 찾는다.

```powershell
.\.venv\Scripts\python.exe scripts\inspect_run.py logs --call "llm_<id>"
```

다음 순서로 읽는다.

1. request의 model, parameters, system, messages, tools
2. response의 content, reasoning content, tool calls, finish reason, usage, error/abort
3. 그 호출과 같은 `stage`·`window_id`를 가진 timeline 이벤트
4. tool event의 `result_ref`가 가리키는 전체 결과 (agentkit 경로일 때)
5. 그 뒤의 결정론적 검증(`domain/maintenance.py`)과 의미 감사 결과, 최종 적용 또는 무변경

raw provider 동작, chunk gap, 잘림이나 반복을 확인해야 할 때만 `streams/<call_id>.jsonl.gz`를
연다. 일반 분류 품질 분석에 수십만 raw chunk를 먼저 읽는 것은 느리고 노이즈가 많다.

`agent_chat` response는 `ok: null`, `attempts: 없음`이 정상이다. 이 형식은 최상위 `stream`과
`finish_reason=tool_calls`를 읽는다. 이를 structured response처럼 `ok != true`로 집계하면 정상 agent
turn을 대량 실패로 오판한다. `operation=structured`인 response만 `ok`와 `attempts[-1]`로 성공률을
계산하고, 두 형식의 finish reason과 usage는 각각의 위치에서 합친다.

### 2.4 전체 도구 결과 확인

timeline의 `result_ref`를 그대로 사용한다.

```powershell
.\.venv\Scripts\python.exe scripts\inspect_run.py logs `
  --artifact "runs/<run_id>/tools/<artifact>.json"
```

timeline의 `preview`는 탐색용일 뿐 증거 원문이 아니다. 모델이 보았던 결과를 판단할 때는 artifact의
`content`와 request의 실제 messages를 함께 확인한다.

도구 결과 artifact는 agentkit 경로(`ask`, `propose_reorg`)에서만 생긴다. 자동 인제스트 경로는
도구를 쓰지 않으므로 그 단계의 증거는 request의 messages 자체다.

## 3. 보고 형식

원인 보고는 각 단계마다 다음 형태를 유지한다.

```text
실제 입력
→ 실제 모델 출력
→ 결정론적 검증 (domain/maintenance.py)
→ 의미 감사 (BoundaryAudit)
→ 적용 또는 무변경
→ 저널 엔트리와 파일시스템 영향
```

각 문장은 다음 중 하나로 표시할 수 있어야 한다.

- **관측:** 로그나 artifact에 직접 존재
- **해석:** 관측과 코드 계약에서 도출
- **미확인:** 필요한 실행 ID 또는 raw 증거가 없음

미확인 가설만으로 프롬프트나 검증기 같은 의미 로직을 변경하지 않는다.

## 4. 로그 자체를 의심할 때

- Windows 탐색기나 `Get-ChildItem`의 표시 크기만 보고 파일이 비었다고 판단하지 않는다.
- `latest.json`의 run ID와 timeline 레코드의 run ID가 같은지 확인한다.
- `llm.call`의 request/response reference가 실제로 열리고 call ID가 일치하는지 확인한다.
- timeline의 tool-result hash와 artifact의 SHA-256이 일치하는지 확인한다.
- manifest에 모델과 생성 설정이 없으면 그 설정에 관한 결론은 미확인으로 둔다.
- API key, Authorization, Cookie를 검색 결과나 보고서에 출력하지 않는다.

## 5. 개선 작업의 완료 조건

코드 변경은 관측된 실패 단계와 일대일로 연결한다. 동일 request 또는 raw trace에서 만든 최소
fixture로 변경 전 실패를 재현하고, 변경 후에는 계약 테스트를 통과시킨다. 실제 Qwen 분류 품질은
사용자가 같은 입력을 다시 실행한 결과로 확인한다. Fake LLM 테스트는 조인 ID, artifact 보존,
검증·적용 안전성을 확인할 뿐 실제 분류 품질의 증거가 아니다.

반복 현상은 [실제 Qwen 반복 사례](2026-08-13-qwen-repetition-incidents.md)의 분류법을 따른다.
`content`가 길다는 이유만으로 반복으로 판정하지 말고, 반복 위치가 일반 prose인지 JSON 문자열인지,
그 시점에 사용 가능했던 종료 도구와 `tool_choice`, item-level schema 제약을 request artifact에서 함께
확인한다.
