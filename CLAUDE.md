# Bismuth — 개발 가이드

> **[`SPEC.md`](SPEC.md)를 먼저 읽는다.** 무엇이 참이어야 하는지는 거기 있고, 이 문서보다
> 위다. 여기 적힌 것이 스펙과 어긋나면 여기가 틀린 것이다.

코드 짜기 전에 읽는 참조. 뭐가 어디 있고, 뭘 깨면 안 되는지만 적는다.

> **작업 시작 전 저장소 루트의 `TEAM.md`를 먼저 읽는다.** 담당 영역과 커밋 규칙이 거기 있다. 브랜치·커밋·병합 규칙은 [`docs/git-workflow.md`](docs/git-workflow.md).

> 규칙: 새 설계 판단은 코드에 에세이로 쓰지 말고 여기 한두 줄로. 소스 주석은 국소적 "왜"(함정·불변식·플랫폼)만 한 줄.

## 제품

문서를 주면 **사서처럼 범주로 나눠 폴더에 넣는다. 나중에 찾기 위해서다.** ([SPEC.md §1](SPEC.md))

폴더 하나를 지켜보다 문서가 들어오면: 파싱 → 모델이 무엇인지 파악 → **기존 폴더 트리를 보고** 배치(없으면 새로 생성) → 원본 옆 grep 가능한 사이드카 + 새 폴더엔 노트. 결과물은 진짜 폴더·파일이라 Bismuth를 지워도 남는다. 사용자 대면 문구는 전부 한국어.

**정리는 두 동작이다: 배치와 세분화** ([SPEC.md §3.4](SPEC.md), [ADR-0008](docs/adr/0008-place-broadly-then-subdivide.md)). 세분화가 없으면 첫 배치가 영구적이 되고 **먼저 들어온 문서가 트리를 결정한다.** 두 번 측정해서 확인됨 — 첫 문서가 `연구논문/…`을 만든 판은 11건 중 8건 배치, `미디어/…`를 만든 판은 15건 중 6건. **차이는 문서 한 건이지 코드 한 줄이 아니었다.** 모델은 기존 최상위 밑에 형제는 만들지만 **새 최상위는 만들지 않는다.**

**첫 문서로 폴더를 만들지 않는다.** 분류는 구분인데 한 건으로는 구분할 게 없다. 루트에 두고, 루트가 커지면 루트를 쪼갠다 — 루트는 특별한 폴더가 아니다.

**임계값으로 판단하지 않는다** ([SPEC.md §6.1](SPEC.md)). "N개 넘으면 쪼갠다" 같은 규칙은 코퍼스마다 맞는 값이 달라 휴리스틱이 된다. 나눌지 말지는 모델이 현재 상태를 보고 판단하고, 숫자는 결과를 **밖에서 재는 자**로만 쓴다.

## 핵심 루프 (`services/ingest.py`)

1. `stage()` — 업로드를 먼저 `_inbox`에 저장(저널). 파싱/모델 실패로 파일을 잃지 않게, 처리보다 저장이 먼저.
2. sha256 중복 검사 — 이미 있으면 배치 않고 기존 위치 반환. 정체성은 파일명이 아니라 바이트.
3. 파싱 → `DocumentCard`(§카탈로깅). **이후 단계는 원문이 아니라 카드만 읽는다.**
4. `placement.decide`(REASONING) — 현재 폴더 트리 전체를 주고 폴더 하나 선택.
5. `_commit` — mkdir + move + 사이드카 write (+ 새 폴더면 노트)를 한 배치로. 부분 실패 없음.
6. `_reconcile_notes` — 영향받은 폴더 노트 다시 그림(§노트 갱신).
7. `subdivision.consider` — 문서가 들어간 폴더를 나눌지 판단(§세분화). 배치의 나머지 반쪽.

## 아키텍처 (헥사고날)

의존 규칙: **domain → ports → adapters/services**. domain은 아무것도 모르고, services는 ports만 알고, adapters가 ports를 구현. `pyproject.toml`의 `banned-api`가 강제. 조립은 `container.py` 한 곳.

- `domain/` — 순수 값 객체·함수(IO 없음).
- `ports/` — Protocol (LLM, Vault, Catalog, Parser, JournalStore).
- `adapters/` — 구현 (filesystem, litellm, jsonl journal, file catalog, parsers).
- `services/` — ingest, placement, **subdivision**, charters, cards, deletion, move, sidecar, transactor.
- `api/`(FastAPI, localhost 전용) · `cli/`(typer) — 둘 다 `container.build`.

## 깨면 안 되는 불변식

**볼트 = 진실.** 디스크의 폴더·파일이 원본. `.bismuth/`는 삭제해도 재생성되는 캐시(카드·배치) — **저널만 예외**. 캐시와 디스크가 갈리면 디스크를 따른다(예: 문서 수는 카드 수가 아니라 디스크 파일 수).

**모든 변경은 Transactor를 거친다** (`services/transactor.py`). 볼트를 바꾸는 유일한 문. 서비스는 Operation 리스트로 의도만 기술. 순서: attic 백업 → 저널 append+fsync(움직이기 전) → 실행 → 실패 시 롤백 → APPLIED. 시작 시 PENDING 엔트리는 inverse로 되감음(`recover`). **Undo = 역연산을 새 저널 엔트리로 실행**(undo도 다시 undo 가능). 저널은 JSONL, `OperationKind`는 작게.

**`confidence`는 "네가 고른 폴더가 맞나"이지 "기존 트리에 맞나"가 아니다.** 이 정의가 프롬프트에 없던 동안 모델은 새 폴더를 만들어야 할 때마다 2~12%로 답했다. 지금은 이 값이 아무것도 막지 않지만(§확신 임계값은 없다) 정의는 여전히 중요하다 — 기록으로 남고, 사람이 읽는다.

**배치는 발명보다 재사용** (`prompts/placement.py`). 맞는 기존 폴더가 새 폴더보다 항상 우선. 드리프트(오늘 `법무/계약`, 내일 `계약서/법무`로 갈라짐) 방어책 = **매번 트리 전체(경로+한 줄 노트)를 모델에 보여줌**. 트리는 폴더 요약이라 폴더 수와 무관하게 저렴(`cache_hint=True`). `folder=""`는 루트(정상 답), `folder=None`은 **읽지 못한 문서**뿐. `created_folder`는 모델 플래그가 아니라 실제 경로 집합으로 판정.

**여기서 재사용을 더 세게 밀어도 안 고쳐지는 게 있다.** 배치는 "지금 트리의 어디"만 답할 수 있고 "이 폴더는 이제 쪼개야 한다"는 답할 수 없다 — 그건 `services/subdivision.py`의 일이다. 폴더가 엉뚱한 것들로 부푸는 증상을 배치 프롬프트로 고치려 들지 말 것.

**확신 임계값은 없다.** 어디 둘지 모르겠으면 답은 대기가 아니라 루트다([SPEC.md §3.4](SPEC.md)). 인박스에 남는 건 **읽지 못한 문서**(`folder=null`)뿐. 확신도는 기록하되 문을 막지 않는다.

**경로 안전** (`domain/paths.py`, `adapters/vault/filesystem.py`). 모델이 준 폴더명이 실제 디렉토리가 됨. `sanitize_segment`가 유일한 관문(Windows 금지문자·예약어, `Q1/Q2`가 두 계층 안 되게, 한글 유지). `vault.resolve`는 resolved 경로로 탈출 검사(`../`, 심링크). 우리가 만든 경로라도 안 믿는다(출처가 공격자 문서일 수 있음).

**사람 노트 불변.** `_folder.md`의 `managed: false`는 읽되 절대 덮어쓰지 않는다.

## 카탈로깅 (`services/cards.py`)

문서 **전체**를 읽는다. 앞 N자만 보던 방식은 100페이지 문서에서 핵심을 통째로 놓쳤음.

루프: 길이로 자른 window를 읽는 순서대로 → 1번은 `CardDraft`, 2번부터는 `CardUpdate`로 **카드를 갱신** → 마지막에 `DensifiedSummary` 1회. 문서당 FAST 호출이 window 수만큼(+1) 든다 — 카드 1회가 아니다.

**형식을 묻지 않는다.** 목차·헤딩·페이지가 있는 문서에서만 도는 방법은 방법이 아니다. 모든 글에 있는 건 순서와 길이뿐이라 자르기는 길이 기준이고, 줄바꿈이 근처에 있으면 거기 맞춰줄 뿐(`Extraction.windows`). 의미 단위 분할(임베딩으로 화제 전환 탐지)은 고정 크기 대비 이득이 없다고 검증된 바 있어 안 쓴다.

**사실은 합집합, 요약은 재작성.** `topics`/`entities`/`keywords`/`answers_questions`는 절대 안 지우고 누적(중복은 casefold/`Entity.key`로 제거) — 순서도 구조도 필요 없는 유일한 병합 방식. 반면 `summary`는 매 window마다 통째로 다시 쓴다(붙이지 않음). `title`/`doc_type`은 모델이 앞선 판단이 틀렸다고 할 때만 교체.

**마지막 densify 1회** = 길이 고정한 채 누적된 사실 중 중요한 걸 요약에 흡수(Chain of Density). 마지막 window는 방금 읽은 텍스트에 쏠려 요약을 쓰므로, 사실 전체를 기준으로 한 번 더 저울질한다. window 1개짜리 문서는 스킵.

**라벨이 아닌 건 자르지 말고 버린다**(`_sift`). 실제 실행에서 `topics`에 참고문헌 목록이 통째로 들어왔다. 40자로 자르면 더 나쁜 라벨이 아니라 **틀린 라벨**이 남고, 그게 사이드카·화면·이후 모든 배치 프롬프트에 실린다. 버린 건 `card.rejected`로 트레이스에 남긴다 — 조용히 걸러내면 카드가 그냥 빈약해 보일 뿐 프롬프트 문제인 걸 모른다.

**커버리지는 카드에 박제**(`Coverage`). `truncated` O/X를 대체 — 몇 조각 중 몇 조각을 읽었고, 그중 몇이 **새 사실을 실제로 줬는지**. contributed는 모델 자기보고(`CardUpdate.contributed`)가 아니라 **새 사실이 붙었는지**로 판정(자기보고는 보일러플레이트에도 yes 함). 둘 다 트레이스에 남겨 불일치가 보이게.

**예산 초과 시 앞부분이 아니라 등간격**(`card_max_windows`, 기본 16). 상한에 걸리면 문서 전체에 고르게 흩뿌려 읽고, 건너뛴 구간을 `card.windows_skipped`로 로그 + 커버리지·사이드카에 명시. 조용한 절단 금지.

window 하나가 실패해도 카드는 유지(첫 window 실패만 치명적).

## 진행 표시 (`domain/progress.py`, `api/progress.py`)

문서 하나가 오래 걸린다(파싱 + window마다 모델 호출 + 배치). "처리 중"만 띄우면 느린 파이프라인이 멈춘 것처럼 보여서, 단계마다 **무엇을 하고 무엇을 찾았는지**를 흘린다.

`ingest.process(rel, on_progress=…)` → `Progress` 값을 순서대로 방출. `report()`는 리스너 예외를 삼킨다 — **UI가 깨져도 인제스트는 안 죽는다**가 계약.

**업로드 POST는 안 건드렸다.** 진행은 별도 SSE(`GET /api/progress`)로 나간다. 기존 API 계약이 유지되고, `/api/scan`(손으로 넣은 파일 재처리)도 공짜로 같은 표시를 받는다. 버스는 인메모리·비영속(로컬 단일 사용자) — 못 따라오는 탭은 **단계를 잃지 인제스트를 막지 않는다**(`QueueFull` 무시).

**`fraction`은 위치와 총량이 둘 다 있어야 성립.** placing은 폴더 수(`steps`)는 알지만 몇 번째인지(`step`)는 모른다 — 이걸 0%로 보내면 읽기 단계가 채운 막대가 뒤로 돌아간다. UI도 한 번 측정된 뒤엔 막대를 되감지 않는다.

한국어 문구는 `Progress.label()`에 둔다(`Coverage.summary_line()`과 같은 자리). 클라이언트가 여럿이어도 같은 말을 하게.

## 세분화 (`services/subdivision.py`)

배치의 나머지 반쪽. 설계는 [`docs/spec/subdivision.md`](docs/spec/subdivision.md)에 있고 아래는 함정만.

인제스트 직후 문서가 들어간 폴더를 본다. **카드만 읽고 원문·요약은 안 읽으며 한 단계 아래까지만** 본다 — 폴더 하나 판단 비용이 장서 크기와 무관해야 하므로.

**폴더를 분할하지 않는다. 자란 부류를 하나씩 꺼낸다**(`Emerging` → `Members`). 분할은 모든 문서를 배정해야 하는 연산이고, 무관한 더미는 배정이 안 돼서 나머지에 이름이 붙는다 — 세 판 연속 `그 밖의 무관한 학술 논문`·`그 밖의 주제`·`기타 주제`가 나왔고 **마지막 것은 프롬프트가 그 단어들을 금지한 상태에서 나왔다.** 문구로는 안 막힌다. 고친 것은 스키마다: 이름 칸이 하나뿐이라 "나머지"를 **표현할 수가 없다.** 안 꺼내진 문서는 부모에 남고 그게 정상이다([SPEC.md §3.4](SPEC.md)). 여기에 문구를 더 얹으려는 충동이 들면 그게 실패한 방법이다.

**질문이 둘이고 이게 되풀이를 막는 핵심이다.** "또 자란 부류가 있나"는 형제를 **더할** 뿐 기존 폴더 간 이동이 없어 진동할 게 없으니 자주 물어도 된다. **경계를 다시 긋는 질문만**(`Review`, 기본 답이 "맞다") 위험하고, 그것만 2배 규칙에 건다. 후자 자리에 "어떻게 나눌까"를 던지면 멀쩡한 구조에도 매번 조금씩 다른 답이 나와 문서가 영원히 움직인다.

**언제 묻나.** 부류 꺼내기는 **매 인제스트마다.** 아끼지 않는다 — 2의 거듭제곱 일정을 걸어봤더니 30건짜리 볼트에서 루트가 2·4·8·16에서 네 번 묻고(전부 정당하게 거절) 32번째 문서를 기다리다 끝나서, **폴더가 하나도 안 생겼다.** 그 일정은 이 질문이 아니라 *앞의* 질문("어떻게 나눌까", 매번 답이 나와서 자주 물으면 "예"로 미끄러짐)을 막으려던 것이다. 지금 질문은 거절하고 계속 거절하므로 아낄 이유가 없다.

경계 재조정만 증거가 두 배 됐을 때(`due_for_review`) — 이미 배치된 문서를 움직이는 유일한 질문이라 그렇다. 폴더가 자기 내력(`split_basis`, `split_at_documents`)을 노트 frontmatter에 기억한다. **재조정 쪽은 하위 전체로 센다** — 직속으로 세면 나누는 순간 0으로 떨어져 영영 다시 안 본다. 둘 다 *언제 물을지*만 정하고 *어떻게*는 모델이 정한다([SPEC.md §6.1](SPEC.md)). **2배 규칙을 부류 꺼내기에까지 걸면 안 된다** — 걸어봤더니 첫 분할이 그대로 최종 트리가 됐다.

**퇴화 케이스 둘은 코드가 막는다**(개수 규칙이 아니라 정의상 구분이 아닌 것들). ① **한 그룹이 전부를 가져가는 건 나눔이 아니다** — 폴더를 한 단계 깊게 옮길 뿐이고 그 아래가 같은 크기의 같은 문제라 무한히 내려간다. ② **자식은 부모와 같은 이름을 못 쓴다** — `철학`에 "뭐가 자랐냐"고 물으면 모델은 `철학`이라 답한다. 맞는 말이라 프롬프트로 안 막히고, ①은 100%를 가져갈 때만 걸려서 5건 중 3건을 가져가면 통과했다(`철학/철학`, `과학·기술 연구/과학·기술 연구`). 둘 다 `subdivide.rejected`로 로그.

**방금 만든 자식으로 재귀하지 않는다.** 한 번 부르면 폴더는 **최대 하나** 생긴다. 그 재귀는 스케줄이 있던 시절 새 폴더가 오래 안 물어질까 봐 있던 것이고, 지금은 매 도착마다 물으므로 **새 증거 없이 방금 한 판단을 다시 하는 것**뿐이다 — 넣어뒀더니 인제스트 한 번에 `철학/현상학/체화된 인지`가 층마다 1건씩 생겼다.

`managed: false`인 폴더는 안 건드린다. 사람이 짠 구조다.

## 폴더 노트(charter, `_folder.md`)

폴더가 **직접 관장하는 것(직속 문서 + 직속 하위폴더)**을 서술. placement 재사용 판단과 에이전트 탐색에 쓰임. frontmatter가 authoritative, 본문은 거기서 생성.

갱신 트리거(`charters.refresh_operations`): ① 문서가 직접 배치 → 그 폴더, ② 새 폴더 생성 → 조상들(자식 얻음), ③ 직속 문서 삭제 → 부모, ④ 하위폴더 삭제 → 부모. **한 단계 위까지만 전파.** 빈 폴더·사람 노트는 스킵. 갱신은 파일 안착 후 별도 저널 배치. 트레이드오프: 갱신마다 REASONING 1회(대량 임포트 시 배치 디듀프가 다음 최적화).

## 그 외 실무 사실

- **모델 프로파일** (`ports/llm.py`): FAST(카탈로깅, 문서당 window 수만큼) / REASONING(배치·노트). 실제 모델은 config 매핑. `DocumentCard.topics`는 고정 카테고리 없는 열린 목록.
- **비용은 문서 단위로 걷는다** (`api/app.py`의 `_drain`). LiteLLM이 호출마다 `completion_cost`로 값을 매기고 `Usage`에 담지만, **이걸 읽는 곳이 오래 없었다** — `drain_usage()` 호출자가 테스트뿐이라 어댑터의 usage 리스트가 프로세스 수명 내내 자라기만 했다. 문서 처리 전후로 걷어서 그 문서 몫으로 귀속시키고, 그게 리스트를 비우는 유일한 장치이기도 하다. **에이전트(`chat.py`) 호출도 센다** — 업로드마다 도는 autoReview를 빼면 합계가 미완이 아니라 틀린 값이 된다. 단가를 모르는 모델(로컬·미등재)은 `cost_usd=None`으로 보고하고 추정하지 않으며, 일부만 값이 매겨졌으면 `priced_calls < calls`로 **총액이 하한선임**을 표시한다(UI는 `+`).
- **지연 임포트는 "언제"의 문제이지 "여부"가 아니다** (`api/app.py`의 `_preload`). 두 개가 늦게 로드된다 — LiteLLM은 python-dotenv의 상위 `.env` 스캔을 우리 설정이 이겨야 해서(`_load_litellm`의 이유), 파서는 `[parsers]` 선택 의존성이라. **서버는 요청을 받기 전에 둘 다 당겨온다.** 임포트에만 4초가 걸리는데 그걸 첫 업로드 안에서 치르면 뜬 서버가 멈춘 걸로 보인다. 없는 선택 파서는 부팅 로그 한 줄이지 치명적 실패가 아니다(최소 설치는 지원되는 실행 방식). 새 파서를 추가하면 `warm()`도 구현해야 하고, 안 하면 그 형식 첫 업로드에서야 터진다.
- **로그** (`logging_setup.py`): `logs/bismuth.log`(사람용) · `logs/llm.jsonl`(모델 호출) · `logs/trace.jsonl`(파이프라인 판단). 셋 다 시작 시 truncate. trace는 **사람이 읽는 문장이 아니라 기계가 재생하는 기록** — 모든 줄에 `t`(UTC)·`event`·`document_id`가 있고, 뒤 둘은 `log_trace`가 `CURRENT_DOCUMENT`에서 알아서 채우므로 **`document_id`로 필터하면 그 문서의 전말이 나온다**(약속만 있고 세분화 줄엔 없던 시절이 있었다). `llm.jsonl`도 같은 두 키를 달아 **두 파일이 조인된다** — 줄 순서로 문서를 짐작하던 방식은 읽기가 병렬이 되면서 죽었다. 새 줄을 추가할 땐 그 줄만 보고 상황이 복원되게: 입력(구간·앞뒤 텍스트)·출력(무엇이 새로 붙었나)·소요시간을 넣고, 그것에 대한 소감은 넣지 않는다. **아무 일도 안 일어난 것도 기록한다**(`subdivide.skipped`) — 안 물어본 것과 묻고 거절한 것이 똑같이 침묵으로 보이면 안 된다.
- **로깅 설정은 uvicorn 뒤에**(`api/app.py`의 lifespan). uvicorn이 `dictConfig`로 자기 로깅을 잡는데 이게 **기존 핸들러를 전부 close** 한다. 로거에 붙어 있고 `disabled`도 False라 예외 하나 없이 조용히 안 써진다 — 서버가 문서 33건을 처리하고 남긴 로그가 두 줄이었다. `create_app`에서 부르면 다시 그렇게 된다.
- **구조화 출력** (`litellm_adapter.structured`): 3-tier 폴백(네이티브 스키마 → JSON 모드 → 프롬프트 임베드). 검증 실패 시 에러를 수리 턴으로 되먹임(소형/로컬 모델 대응).
- **설정** (`config.py`): 자기 키 하나만 읽음(`Settings.api_key`). `OPENAI_API_KEY` 등 앰비언트 env는 안 읽고 LiteLLM엔 명시 인자로 전달. 우선순위: 명시 인자 > `BISMUTH_*` > `./.env` > `~/.bismuth/config.json` > 기본값.
- **파일시스템** (`adapters/vault/filesystem.py`): 원자적 쓰기(같은 디렉토리 temp → `os.replace`). `unique_target`은 case-insensitive(Linux `Report.pdf`/`report.pdf`가 Windows에서 충돌 안 나게). RMDIR 비재귀. 사이드카·노트는 문서 카운트에서 제외.
- **파서** (`adapters/parsers/`): 확장자 기반, 등록 first-wins. `.hwpx`만(레거시 `.hwp` 아님). 깨진 추출은 실패 처리.
- **일괄 삭제** (`services/deletion.py`): 여러 폴더도 **저널 엔트리 하나** — 폴더 3개 지웠는데 undo를 3번 해야 하면 약속한 것보다 나쁜 거래다. `_outermost`가 상위에 이미 포함된 선택을 흡수(트리에서 부모+자식 동시 선택은 정상 행동이고, 안 흡수하면 문서가 두 번 세어지고 같은 디렉토리에 RMDIR이 두 번 걸린다). 하나라도 잘못된 경로(루트·인박스·없는 폴더)면 아무것도 안 지운다. UI는 트리 ctrl/shift+클릭(평범한 클릭은 그대로 열기), 확인창에 **실제 삭제될 폴더·문서 개수**를 세어 보여준다.

## 에이전트 (agentkit)

에이전트 기능은 **`packages/agentkit`** — bismuth와 분리된 독립 라이브러리(자체 pyproject, 의존성 pydantic만, bismuth를 절대 import 안 함=테스트로 강제). bismuth가 **설치된 라이브러리로 import**한다. 나중에 별도 배포 예정. 신규 환경 셋업: `pip install -e packages/agentkit`.

- 코어: `Agent` 루프(도구 없으면 종료·병렬 실행·max_turns·이벤트), `Tool`/`@tool`(권한 게이트: 읽기 ALLOW/변경 ASK), `subagent_tool`(격리 위임·깊이 제한·이벤트 forward), `ChatModel` Protocol + `FakeModel`.
- bismuth 쪽 배선(`bismuth/services/agent.py`, `adapters/llm/chat.py`): LiteLLMChatModel(네이티브 tool-calling) + 볼트 툴. `ask`(읽기 Q&A) / `propose_reorg`(구조 정리 제안: 읽기+기록형 move/rename 툴 + verifier 서브에이전트, **에이전트는 제안만 하고 실행은 승인 후 bismuth가**).
- 구조 정리 원칙: 개수 규칙 없이 LLM 판단([SPEC.md §6.1](SPEC.md)), **노트를 믿지 말고 실제 doc_type·이름 정합성으로**(노트는 자가 치유돼 드리프트를 가림), move/rename은 저널·undo. 업로드 후 자율 탐지(`autoReview`).
- **`propose_reorg`의 승인 게이트는 스펙과 어긋난다.** [SPEC.md §5](SPEC.md)는 사람의 개입을 현재 범위 밖으로 미뤘고, 사서 역할은 AI가 한다. 이 에이전트가 §3.4의 세분화를 맡을지, 아니면 세분화가 별도 경로로 들어가고 이건 다른 용도로 남을지는 [SPEC.md §7](SPEC.md)에서 미정 — 로직 정리 때 결정한다.

## 개발·테스트

- **FakeLLM + 임시 디렉토리**로 전 엔진을 네트워크·키·비용 없이 결정적 실행(`tests/conftest.py`의 `ScriptedModel`은 schema-keyed라 호출 수 바뀌어도 안 깨짐). agentkit은 `agentkit.testing.FakeModel`.
- 커밋 전: `ruff check src tests packages/agentkit` · `mypy src` · `pytest` 모두 통과. agentkit 단독: `cd packages/agentkit && mypy && pytest`. venv: `.venv/Scripts/python.exe`.
- 삭제 서비스와 API delete/organize 엔드포인트는 async.
