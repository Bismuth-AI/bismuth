# Agent 구조 개발 계획

목표: bismuth에 에이전트 기능을 붙이되, **에이전트 코어는 bismuth와 분리된 독립 라이브러리(`agentkit`)로** 개발한다. 나중에 별도 패키지로 배포하고 bismuth가 `import agentkit` 해서 쓴다. (로컬 LLM 지원은 당분간 고려 안 함 — 네이티브 tool-calling 사용.)

레퍼런스: `side_project/reference/deepagents`(3핵심: tool-loop·sub-agent·todos), `side_project/reference/claude-code-source-code`(Tool 인터페이스·권한 게이트·fs 툴·Task 재귀·fail-closed 기본값).

## 상태: 라이브러리로 추출 완료

`agentkit`은 **`packages/agentkit/`** 의 독립 설치형 패키지로 분리됨(자체 `pyproject.toml`·README·LICENSE·`py.typed`). 자체 wheel 빌드·mypy·pytest 통과. bismuth는 이를 **editable 설치(`pip install -e packages/agentkit`)해 설치된 라이브러리로 import**한다. 나중에 그대로 별도 배포 가능.

## 경계 규칙

- **`agentkit`는 bismuth를 절대 import하지 않는다.** 범용·재사용 가능해야 함(테스트로 강제).
- 코어는 LLM-agnostic: `ChatModel` Protocol만 의존. litellm 등 구체 모델은 어댑터(소비자 쪽)가 주입.
- 코어 런타임 의존성은 `pydantic`뿐.

## 구성 (`src/agentkit/`)

- `messages.py` — provider-neutral 메시지/툴콜 타입 (`Message`, `ToolCall`, `AssistantMessage`, `ToolSpec`).
- `model.py` — `ChatModel` Protocol (`async complete(system, messages, tools) -> AssistantMessage`).
- `tool.py` — `Tool` Protocol + `FunctionTool` + `@tool` 데코레이터, `Permission`(ALLOW/ASK/DENY), fail-closed 기본값.
- `registry.py` — `ToolRegistry` (name→tool, specs).
- `loop.py` — `Agent` (continue-iff-tool_use 루프, max_turns, dispatch 파이프라인: parse→permission→run, on_ask 콜백, 이벤트).
- `subagent.py` — `task` 툴 (등록된 서브에이전트를 격리 컨텍스트로 실행, 최종 텍스트만 반환).
- `todos.py` — `write_todos` 계획 툴(선택).
- `testing.py` — 스크립트 FakeModel(테스트·오프라인용).

## Phase

1. **[done] 코어**: `agentkit` — messages·model·tool·registry·loop(권한게이트·이벤트·max_turns) + FakeModel + 서브에이전트(`task`). bismuth import 0(경계 테스트). 15 tests.
2. **[done] litellm 어댑터 + 볼트 탐색 툴(읽기전용)**: `bismuth/adapters/llm/chat.py`(LiteLLMChatModel) + `bismuth/services/agent.py`(ls·tree·read·grep[사이드카]·read_note + AgentService). container 배선. 통합 테스트(FakeModel로 실제 볼트 툴 구동).
3. **[done] 구조 정리 에이전트(제안→승인→적용)**: `AgentService.propose_reorg`(읽기 툴 + 기록형 move 툴 = 에이전트는 제안만, 실행 못 함) + `verifier` 서브에이전트. API `/api/organize/propose`·`/api/organize/apply`. UI "구조 정리" 버튼 + 제안 검토 모달 + 적용(→MoveService, 저널·undo). 개수 규칙 없이 LLM 판단.
4. **[부분] 서브에이전트**: `task` + `verifier` 배선 완료. planner 분리·todos는 실전 튜닝 후.
5. **[deferred] 자동 파수꾼(on-ingest)**: 온디맨드 "구조 정리"가 곧 LLM 판단 파수꾼이라 우선 그것으로. 매 ingest 자동 트리거는 hot-path 비용이라 프롬프트 튜닝 뒤 추가.
6. **[deferred] 질의응답(ask) UI**: 백엔드(`AgentService.ask`)는 있으나 UI 미노출(폴더구조 집중).

## 안 가져오는 것

LangGraph/LangChain, harness profiles, prompt-caching·summarization 미들웨어, skills/memory, sandbox execute, async/background 에이전트, 로컬모델 구조화-출력 폴백(당분간).
