# Qwen 출력 안정화 테스트 결과

이 문서는 Qwen의 구조화 출력 붕괴와 반복 출력을 조사하면서 실제로 수행한
테스트, 결과, 남은 검증 항목을 한곳에 기록한다. 수치는 추측이나 회고가 아니라
아래 원시 산출물에서 집계한 값이다.

- 실행일: 2026-08-11
- 모델: `qwen3.6-35b`
- 엔드포인트: OpenAI 호환 vLLM 0.24.0
- 실험 원시 결과: `logs/llm-guard-experiment.jsonl`
- 실험 요약: `logs/llm-guard-experiment-summary.json`
- 실험 코드: `scratch/llm_guard_experiment.py`
- 분석 코드: `scratch/analyze_guard_results.py`

원시 결과에는 요청과 응답 chunk가 포함되므로 외부에 공유하기 전에 문서 내용과
인증 관련 필드를 반드시 점검해야 한다. API key와 gateway Cookie는 이 보고서에
기록하지 않는다.

## 1. 최초 장애 재현

실제 실패했던 Placement 요청을 동일한 system prompt, user prompt, 모델 설정,
native response schema로 재생했다.

| 항목 | 결과 |
|---|---:|
| 입력 토큰 | 641 |
| 출력 토큰 | 64,895 |
| 전체 context | 65,536 |
| stream chunk | 64,757 |
| 실행 시간 | 약 358초 |
| finish reason | `length` |
| reasoning 출력 | 없음 |

겉으로는 답을 이미 출력한 뒤에도 `]}` 또는 `<answer> F002</answer>` 계열 조각을
반복했다. chunk가 계속 도착했기 때문에 inactivity timeout만으로는 중단할 수 없는
생성 반복이었다. 보존된 실행 로그에서는 64,000 출력 토큰을 넘긴 Placement 시도가
6건 확인됐다.

## 2. 실제 Qwen 대조 실험

모든 Placement 실험은 같은 실패 요청을 사용했고 정답은 `F002`였다.

| 실험 | 횟수 | 성공 | 결과 |
|---|---:|---:|---|
| Native JSON schema, `max_tokens=16`, temperature 0.7 | 5 | 0 | 5회 모두 잘린 비정상 JSON, `finish_reason=length` |
| Response schema 제거, 원래 prompt, `max_tokens=16` | 10 | 10 | 모두 `F002` 반환 |
| 명시적 allow-list와 plain ID, temperature 0.7 | 10 | 10 | 모두 `F002` 반환 |
| 명시적 allow-list와 plain ID, temperature 0 | 10 | 10 | 모두 `F002` 반환 |
| vLLM `structured_outputs.choice`, temperature 0 | 10 | 10 | 모두 정답, provider 종속 방식 |
| Native 요청과 client guard, 출력 상한 미지정 | 3 | 3 | 이 3회에서는 반복이 재발하지 않고 정상 종료 |
| 잘린 primary 뒤 clean plain-ID retry | 5 | 5 | 5회 모두 retry에서 복구 |

여기서 `max_tokens=16`만 적용한 native JSON 방식은 runaway 시간은 제한했지만
유효한 응답을 만들지는 못했다. 가장 안정적인 범용 경로는 schema가 없는 plain-ID
선택이었고, provider 종속 기능을 허용한다면 vLLM choice도 안정적이었다.

Native 요청과 client guard를 사용한 3회가 모두 정상 종료한 것은 모델 출력의
비결정성을 보여준다. 이 3회만으로 guard가 실제 stream을 중단했다고 해석하면 안
된다. guard의 탐지 성능은 다음 절의 보존 runaway 재생으로 따로 확인했다.

## 3. 저장된 runaway에 대한 반복 탐지 재생

짧은 비공백 suffix가 정확히 6회 반복되는지 검사하는 탐지기를 보존된 runaway
6건 전체에 오프라인으로 재생했다.

| 대상 | 결과 |
|---|---:|
| 보존 runaway | 6건 모두 탐지 |
| 39 출력 문자 안에 탐지 | 5건 |
| 나머지 1건 탐지 시점 | 1,116 출력 문자 |
| 정상 실험 응답 | 72건 |
| 정상 응답 오탐 | 0건 |

이는 저장된 장애 유형에 대한 회귀 관찰값이다. 모든 자연어 출력에서 오탐이 절대
없다는 증명은 아니다.

## 4. 다른 구조화 schema와 제한된 repair

Placement에 맞춘 작은 출력 상한을 모든 schema에 일괄 적용하면 정상 응답까지
자를 수 있으므로 대표 schema를 별도로 시험했다.

| schema/경로 | 출력 상한 | 횟수 | 유효 응답 |
|---|---:|---:|---:|
| `CharterDraft` | 128 | 5 | 5/5 |
| `CardDraft` | 1,024 | 3 | 3/3 |
| `BoundaryAudit` | 512 | 3 | 3/3 |
| 100,012자짜리 synthetic invalid 응답 중 2,000자만 repair context로 전달 | 128 | 3 | 3/3 |

추가로 inactivity timeout과 absolute timeout의 결정론적 stream 테스트가 모두
통과했다. 실험 전체 62회에서 application-visible raw chunk 누락은 0건이었다.

## 5. 코드에 남긴 회귀 테스트

`tests/test_litellm_adapter.py`에는 실제 네트워크를 호출하지 않는 fake stream 기반
테스트가 있다.

- Agent Chat의 4,096 token hard ceiling
- 짧은 exact suffix 반복 중단
- 공백만 계속 출력되는 stream 중단
- LiteLLM/provider가 감싼 반복 오류의 정규화
- 구조화 호출 반복 중단 후 native schema를 제거한 clean retry
- 긴 Agent prose에서 동일한 8단어 이상 문장 틀이 반복되는 경우 중단
- 반복 중단된 Agent prose를 전체 window 실패로 만들지 않고
  `finish_reason=repetition_guard`로 반환

이 테스트들은 반복 방어 코드의 계약을 검증하지만 실제 Qwen 생성 품질을 검증하는
라이브 실험은 아니다.

## 6. 현재 적용된 결론

- Placement는 JSON schema 대신 bounded plain-choice 프로토콜을 사용한다.
- 구조화 schema별로 서로 다른 token ceiling을 적용한다.
- `finish_reason=length`는 JSON repair가 아니라 더 적절한 상한의 clean retry 대상으로
  취급한다.
- 연속 공백, 짧은 suffix 반복, 긴 단어 열 반복을 client에서 탐지한다.
- inactivity timeout과 별도로 absolute generation timeout을 둔다.
- transport retry는 0으로 유지해 버려진 생성을 서버에서 중복 실행하지 않는다.
- raw stream은 진단용으로 보존하되 repair prompt에 넣는 실패 출력은 제한한다.

## 7. 아직 남은 검증

2026-08-13에 추가한 긴 Agent prose 반복 탐지는 fake stream 단위 테스트까지
완료됐지만, 수정 후 실제 Qwen으로 동일 실패 stage를 재생하는 검증은 아직 하지
않았다. 따라서 아래 두 결과를 구분해야 한다.

- 구조화 Placement 반복 대응: 실제 Qwen 대조 실험과 보존 runaway 재생 완료
- 최근 Agent 장문 반복 대응: 단위 회귀 테스트 완료, 수정 후 live Qwen 검증 미완료

## 8. 2026-08-13 연결 점검

현재 사용자 설정의 `custom / qwen3.6-35b` 모델과 OpenAI 호환 endpoint에 짧은
non-streaming 요청을 보냈다. 인증정보 자체는 이 문서에 기록하지 않았다.

첫 점검은 PowerShell `Invoke-RestMethod`와 `Invoke-WebRequest`를 사용했고 두 요청 모두
`401 Unauthorized`, `Not Authenticated - INVALIDCOOKIE`를 반환했다. 처음에는 저장된
Cookie가 유효하지 않다고 판단했으나, 이 판단은 잘못이었다.

사용자가 같은 값으로 Postman에서 성공한다고 알려준 뒤 Windows `curl.exe`로 동일한
Cookie, Bearer token, endpoint와 body를 다시 전송했다.

| HTTP client | Postman User-Agent | 결과 |
|---|---:|---|
| PowerShell `Invoke-RestMethod` | 없음 | `401 INVALIDCOOKIE` |
| PowerShell `Invoke-WebRequest` | 없음 | `401 INVALIDCOOKIE` |
| Windows `curl.exe` | 있음 | `200`, Qwen 정상 응답 |
| Windows `curl.exe` | 없음 | `200`, Qwen 정상 응답 |

따라서 인증정보와 endpoint는 유효하며 User-Agent도 성공 조건이 아니다. 실패 원인은
PowerShell HTTP client의 Cookie 전송 방식과 KT gateway 사이의 호환 문제로 좁혀진다.
Bismuth는 PowerShell HTTP client를 사용하지 않고 LiteLLM/Python transport를
사용하므로, 앞선 `401`은 프로젝트 설정 실패의 증거가 아니다.

이 점검은 일반 chat completion 연결만 확인했다. 최근 Agent 장문 반복 방어를 실제
Qwen 실패 stage에 적용하는 live replay는 별도 검증으로 여전히 남아 있다.

## 9. 2026-08-13 최소 Agent tool-call 점검

현재 프로젝트의 생성 설정으로 Qwen이 Agent 결론 도구를 호출할 수 있는지 1회
비파괴 점검했다.

- 설정: temperature 0.2, top_p 0.8, presence penalty 0, top_k 20, min_p 0,
  thinking off
- 출력 상한: 256 token
- 입력: 은행 규정 문서 `D0001`, `D0002`를 하나의 한국어 shelf로 묶고 설명문 대신
  `submit_plan`을 정확히 한 번 호출하라는 최소 prompt
- 실제 볼트와 checkpoint 변경: 없음

| 항목 | 결과 |
|---|---|
| HTTP/model 응답 | 성공, `qwen3.6-35b` |
| finish reason | `tool_calls` |
| tool-call 수 | 1 |
| 호출 도구 | `submit_plan` |
| 제안 shelf | `은행 규정` |
| 제출 문서 | `D0001`, `D0002` |
| 도구 외 prose | 없음 |
| token 사용량 | 입력 358, 출력 54 |

결론 도구를 호출하는 기본 Agent 호환성은 확인됐다. 다만 이 최소 요청은 실제 30개
window, read tool 왕복, critic, validator, 반복 guard가 연결된 전체 구조 정리 성공을
보장하지 않는다. 그 동작은 실제 실패 stage의 bounded live replay 또는 사용자 업로드
실행에서 별도로 확인해야 한다.

## 관련 문서

## 10. 2026-08-13 production organizer 계약 live check

`scripts/live_agent_contract_check.py`로 사용자 볼트를 읽거나 변경하지 않는 네 장짜리 합성 창을
만들고, 프로젝트의 실제 `qwen3.6-35b` 설정과 production `SYSTEM_ORGANIZE`, tool schema,
context policy를 그대로 사용했다. 이미 `금융/시행규칙`에 있는 `D000001`과 루트의
`D000002`는 `F001`, 루트의 `D000003,D000004`는 `F002`로 표시했다.

첫 재검증에서는 합성 fixture 자체의 한글이 mojibake로 저장된 사실을 발견했다. 이는 provider
실패가 아니라 테스트 데이터 결함이므로 fixture를 정상 UTF-8 한글로 교체하고 같은 호출을 다시
실행했다. 최종 결과는 다음과 같다.

| 항목 | 실제 결과 |
|---|---|
| model 연결 | 성공 |
| 종료 | `conclusion`, 5 turns |
| 유효 plan 제출 | 1회 |
| 검증 거절 | 0회 |
| 기존 올바른 문서 | `D000001` no-op 이동에서 제외 |
| 갈라진 family 교정 | `D000002 → 금융` |
| 새 family | `D000003,D000004 → 연구` 함께 이동 |
| operation | root `add_sibling` |
| 한글 axis/question | `정책분야` / `문서의 정책 분야는 무엇입니까?` |

Qwen은 차단될 동일 `tree` 호출을 반복해서 생성했지만 context guard가 실행 결과를 재사용하지
않고 막았으며, 최종적으로 추가 거절 없이 정확한 `submit_plan` 하나를 냈다. 따라서 반복 성향은
완전히 사라진 것이 아니지만 출력 한도·동일 호출 차단·결론 도구 예약의 조합으로 계약 안에서
종료됨을 확인했다.

- `docs/troubleshooting/structured-output-loops.md`: 장애 원인과 운영 진단 절차
- `docs/adr/0013-bounded-llm-output-and-plain-placement.md`: 설계 결정
- `docs/troubleshooting/log-debugging.md`: run/call 단위 raw 로그 분석 방법
