# Bismuth — 개발 가이드

코드 짜기 전에 읽는 참조. 뭐가 어디 있고, 뭘 깨면 안 되는지만 적는다.

> **작업 시작 전 저장소 루트의 `TEAM.md`를 먼저 읽는다.** 담당 영역과 커밋 규칙이 거기 있다. 브랜치·커밋·병합 규칙은 [`docs/git-workflow.md`](docs/git-workflow.md).

> 규칙: 새 설계 판단은 코드에 에세이로 쓰지 말고 여기 한두 줄로. 소스 주석은 국소적 "왜"(함정·불변식·플랫폼)만 한 줄.

## 제품

폴더 하나를 지켜보다 문서가 들어오면: 파싱 → 모델이 무엇인지 파악 → **기존 폴더 트리를 보고** 배치(없으면 새로 생성) → 원본 옆 grep 가능한 사이드카 + 새 폴더엔 노트. 결과물은 진짜 폴더·파일이라 Bismuth를 지워도 남는다. 사용자 대면 문구는 전부 한국어.

## 핵심 루프 (`services/ingest.py`)

1. `stage()` — 업로드를 먼저 `_inbox`에 저장(저널). 파싱/모델 실패로 파일을 잃지 않게, 처리보다 저장이 먼저.
2. sha256 중복 검사 — 이미 있으면 배치 않고 기존 위치 반환. 정체성은 파일명이 아니라 바이트.
3. 파싱 → `DocumentCard`(모델 1회, FAST). **이후 단계는 원문이 아니라 카드만 읽는다.**
4. `placement.decide`(REASONING) — 현재 폴더 트리 전체를 주고 폴더 하나 선택.
5. `_commit` — mkdir + move + 사이드카 write (+ 새 폴더면 노트)를 한 배치로. 부분 실패 없음.
6. `_reconcile_notes` — 영향받은 폴더 노트 다시 그림(§노트 갱신).

## 아키텍처 (헥사고날)

의존 규칙: **domain → ports → adapters/services**. domain은 아무것도 모르고, services는 ports만 알고, adapters가 ports를 구현. `pyproject.toml`의 `banned-api`가 강제. 조립은 `container.py` 한 곳.

- `domain/` — 순수 값 객체·함수(IO 없음).
- `ports/` — Protocol (LLM, Vault, Catalog, Parser, JournalStore).
- `adapters/` — 구현 (filesystem, litellm, jsonl journal, file catalog, parsers).
- `services/` — ingest, placement, charters, cards, deletion, sidecar, transactor.
- `api/`(FastAPI, localhost 전용) · `cli/`(typer) — 둘 다 `container.build`.

## 깨면 안 되는 불변식

**볼트 = 진실.** 디스크의 폴더·파일이 원본. `.bismuth/`는 삭제해도 재생성되는 캐시(카드·배치) — **저널만 예외**. 캐시와 디스크가 갈리면 디스크를 따른다(예: 문서 수는 카드 수가 아니라 디스크 파일 수).

**모든 변경은 Transactor를 거친다** (`services/transactor.py`). 볼트를 바꾸는 유일한 문. 서비스는 Operation 리스트로 의도만 기술. 순서: attic 백업 → 저널 append+fsync(움직이기 전) → 실행 → 실패 시 롤백 → APPLIED. 시작 시 PENDING 엔트리는 inverse로 되감음(`recover`). **Undo = 역연산을 새 저널 엔트리로 실행**(undo도 다시 undo 가능). 저널은 JSONL, `OperationKind`는 작게.

**배치는 발명보다 재사용** (`prompts/placement.py`). 맞는 기존 폴더가 새 폴더보다 항상 우선. 드리프트(오늘 `법무/계약`, 내일 `계약서/법무`로 갈라짐) 방어책 = **매번 트리 전체(경로+한 줄 노트)를 모델에 보여줌**. 트리는 폴더 요약이라 폴더 수와 무관하게 저렴(`cache_hint=True`). 신뢰도 < `placement_min_confidence`(0.65)거나 `folder=None`이면 인박스 파킹(→`scan`으로 재처리). `created_folder`는 모델 플래그가 아니라 실제 경로 집합으로 판정. (구조 성숙/소급 재정비는 아직 없음 — 향후 별도 "제안-승인" 방식으로.)

**경로 안전** (`domain/paths.py`, `adapters/vault/filesystem.py`). 모델이 준 폴더명이 실제 디렉토리가 됨. `sanitize_segment`가 유일한 관문(Windows 금지문자·예약어, `Q1/Q2`가 두 계층 안 되게, 한글 유지). `vault.resolve`는 resolved 경로로 탈출 검사(`../`, 심링크). 우리가 만든 경로라도 안 믿는다(출처가 공격자 문서일 수 있음).

**사람 노트 불변.** `_folder.md`의 `managed: false`는 읽되 절대 덮어쓰지 않는다.

## 폴더 노트(charter, `_folder.md`)

폴더가 **직접 관장하는 것(직속 문서 + 직속 하위폴더)**을 서술. placement 재사용 판단과 에이전트 탐색에 쓰임. frontmatter가 authoritative, 본문은 거기서 생성.

갱신 트리거(`charters.refresh_operations`): ① 문서가 직접 배치 → 그 폴더, ② 새 폴더 생성 → 조상들(자식 얻음), ③ 직속 문서 삭제 → 부모, ④ 하위폴더 삭제 → 부모. **한 단계 위까지만 전파.** 빈 폴더·사람 노트는 스킵. 갱신은 파일 안착 후 별도 저널 배치. 트레이드오프: 갱신마다 REASONING 1회(대량 임포트 시 배치 디듀프가 다음 최적화).

## 그 외 실무 사실

- **모델 프로파일** (`ports/llm.py`): FAST(문서당 카탈로깅) / REASONING(배치·노트). 실제 모델은 config 매핑. `DocumentCard.topics`는 고정 카테고리 없는 열린 목록.
- **구조화 출력** (`litellm_adapter.structured`): 3-tier 폴백(네이티브 스키마 → JSON 모드 → 프롬프트 임베드). 검증 실패 시 에러를 수리 턴으로 되먹임(소형/로컬 모델 대응).
- **설정** (`config.py`): 자기 키 하나만 읽음(`Settings.api_key`). `OPENAI_API_KEY` 등 앰비언트 env는 안 읽고 LiteLLM엔 명시 인자로 전달. 우선순위: 명시 인자 > `BISMUTH_*` > `./.env` > `~/.bismuth/config.json` > 기본값.
- **파일시스템** (`adapters/vault/filesystem.py`): 원자적 쓰기(같은 디렉토리 temp → `os.replace`). `unique_target`은 case-insensitive(Linux `Report.pdf`/`report.pdf`가 Windows에서 충돌 안 나게). RMDIR 비재귀. 사이드카·노트는 문서 카운트에서 제외.
- **파서** (`adapters/parsers/`): 확장자 기반, 등록 first-wins. `.hwpx`만(레거시 `.hwp` 아님). 깨진 추출은 실패 처리.

## 에이전트 (agentkit)

에이전트 기능은 **`packages/agentkit`** — bismuth와 분리된 독립 라이브러리(자체 pyproject, 의존성 pydantic만, bismuth를 절대 import 안 함=테스트로 강제). bismuth가 **설치된 라이브러리로 import**한다. 나중에 별도 배포 예정. 신규 환경 셋업: `pip install -e packages/agentkit`.

- 코어: `Agent` 루프(도구 없으면 종료·병렬 실행·max_turns·이벤트), `Tool`/`@tool`(권한 게이트: 읽기 ALLOW/변경 ASK), `subagent_tool`(격리 위임·깊이 제한·이벤트 forward), `ChatModel` Protocol + `FakeModel`.
- bismuth 쪽 배선(`bismuth/services/agent.py`, `adapters/llm/chat.py`): LiteLLMChatModel(네이티브 tool-calling) + 볼트 툴. `ask`(읽기 Q&A) / `propose_reorg`(구조 정리 제안: 읽기+기록형 move/rename 툴 + verifier 서브에이전트, **에이전트는 제안만 하고 실행은 승인 후 bismuth가**).
- 구조 정리 원칙: 개수 규칙 없이 LLM 판단, **노트를 믿지 말고 실제 doc_type·이름 정합성으로**(노트는 자가 치유돼 드리프트를 가림), 승인 게이트 + move/rename(저널·undo). 업로드 후 자율 탐지(`autoReview`).

## 개발·테스트

- **FakeLLM + 임시 디렉토리**로 전 엔진을 네트워크·키·비용 없이 결정적 실행(`tests/conftest.py`의 `ScriptedModel`은 schema-keyed라 호출 수 바뀌어도 안 깨짐). agentkit은 `agentkit.testing.FakeModel`.
- 커밋 전: `ruff check src tests packages/agentkit` · `mypy src` · `pytest` 모두 통과. agentkit 단독: `cd packages/agentkit && mypy && pytest`. venv: `.venv/Scripts/python.exe`.
- 삭제 서비스와 API delete/organize 엔드포인트는 async.
