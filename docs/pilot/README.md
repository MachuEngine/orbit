# 모델 파일럿 실행 가이드 (OpenAI 직접 호출)

근거: [ADR-0014](../adr/ADR-0014-openai-direct-for-pilot.md), ADR-0009 §1·§3. 합성 데이터만 쓴다.
상태: 실행 준비 단계다. 아래 명령은 아직 실제 API로 실행해 보지 않았다.

## 환경 변수

| 변수 | 값 |
| --- | --- |
| `ORBIT_LLM_PROVIDER` | `openai` |
| `ORBIT_OPENAI_API_KEY` | 개발자가 `secrets/.env`에 둔다. 명령이나 문서에 쓰지 않는다 |
| `ORBIT_LLM_MODEL_ID` | `openai/<모델>` 형식. 후보 표 참고 |
| `ORBIT_LLM_REASONING_EFFORT` | `none` / `low` 등. 비우면 보내지 않는다 |
| `ORBIT_LLM_SEND_TEMPERATURE` | 기본 `false`(보내지 않음, 지문에 기록) |
| `ORBIT_LLM_PRICE_INPUT_PER_MTOK`, `ORBIT_LLM_PRICE_CACHED_INPUT_PER_MTOK`, `ORBIT_LLM_PRICE_OUTPUT_PER_MTOK` | 1백만 토큰당 달러 |
| `ORBIT_LLM_PRICE_SOURCE` | 단가 출처 문자열 |

선택: `ORBIT_OPENAI_BASE_URL`, `ORBIT_LLM_TIMEOUT_SECONDS`, `ORBIT_LLM_MAX_RETRIES`,
`ORBIT_LLM_ACCEPTED_RESPONSE_MODEL_IDS`. 단가표는 네 값이 모두 있어야 하고, 없으면 비용이 미상이 되어
`--max-cost-usd`만으로는 실행이 중단된다(`--max-total-tokens`를 함께 준다).

## 후보 모델 3안

단가(1M 토큰당 달러, 입력/캐시 입력/출력)의 출처는 developers.openai.com/api/docs/pricing(2026-11-11 확인)이다.

| 안 | `ORBIT_LLM_MODEL_ID` | effort | 입력 | 캐시 | 출력 |
| --- | --- | --- | --- | --- | --- |
| A | `openai/gpt-6-luna` | `none` | 0.10 | 0.01 | 0.50 |
| B | `openai/gpt-6-luna` | `low` | 0.10 | 0.01 | 0.50 |
| C | `openai/gpt-6.1-terra` | `low` | 1 | 0.05 | 5 |

## 실행

저장소 루트에서 실행한다. 키는 개발자가 먼저 환경에 불러온다.

```bash
set -a; source secrets/.env; set +a
cd backend
export ORBIT_LLM_PROVIDER=openai
export ORBIT_LLM_PRICE_SOURCE="developers.openai.com/api/docs/pricing (2026-11-11)"

# 안 A (B는 effort만 low, C는 모델·단가·effort를 바꾼다)
export ORBIT_LLM_MODEL_ID=openai/gpt-6-luna
export ORBIT_LLM_REASONING_EFFORT=none
export ORBIT_LLM_PRICE_INPUT_PER_MTOK=0.10
export ORBIT_LLM_PRICE_CACHED_INPUT_PER_MTOK=0.01
export ORBIT_LLM_PRICE_OUTPUT_PER_MTOK=0.50
```

### 1. 사전 측정 (과제 5개 x 1회)

결과는 `<out 이름>.estimate.jsonl`에 따로 쓰고, 에피소드당 평균 토큰·비용과 전체 추정치를 출력한다.
추정은 표본 평균의 외삽이라 과제별 편차는 반영하지 않는다. 출력된 추정 비용을 개발자가 확인한 뒤 본 실행한다.
출력의 `length_finish_rate`는 전체 LLM 호출 중 출력 토큰 한도로 끝난 응답의 비율이다. 0이 아니면 답이
잘렸다는 뜻이므로 한도를 올리거나 결과 해석에 반영한다.

```bash
uv run python -m orbit.evalrunner run \
  --dataset ../eval/datasets/orbit-eval/v0 --split dev --sut assistant_v0 \
  --live-llm --estimate 5 --repeats 3 --seed 0 --prompt-version assistant_v0 \
  --max-cost-usd 0.5 --max-total-tokens 2000000 \
  --out ../runs/pilot/A.jsonl
```

### 2. 본 실행

```bash
uv run python -m orbit.evalrunner run \
  --dataset ../eval/datasets/orbit-eval/v0 --split dev --sut assistant_v0 \
  --live-llm --repeats 3 --seed 0 --prompt-version assistant_v0 \
  --max-cost-usd 2.5 --max-total-tokens 20000000 \
  --out ../runs/pilot/A.jsonl
```

- 상한 정하는 방법: `--max-cost-usd`는 ADR-0014의 파일럿 전체 8달러를 안 수로 나눈 값을 넘지 않게 두고,
  사전 측정의 `estimated_total_cost_usd`에 여유를 곱한 값(예: 1.5배)으로 정한다. `--max-total-tokens`는
  `estimated_total_tokens`에 같은 여유를 곱한 값이다. 단가를 설정하지 않으면 비용이 미상이라 토큰 상한이
  유일한 안전장치가 된다. 여유 배수는 근거 있는 값이 아니라 개발자가 정하는 값이다.
- 누적 상한(`--max-cost-usd`, `--max-total-tokens`)을 넘으면 중단하고 종료 코드 3을 낸다. 파일럿 전체 상한은
  ADR-0014의 8달러 이하이므로 안마다 나눠 설정한다. 위 숫자는 예시이고, 사전 측정 결과로 정한다.
- `test` 분할은 봉인돼 있어 파일럿에 쓰지 않는다.
- `runs/`는 git에서 제외된다. 결과 파일을 커밋하지 않는다.
- 통계는 `python -m orbit.evalrunner stats ../runs/pilot/A.jsonl --compare ../runs/pilot/B.jsonl`로 낸다.

## 결과에 남는 설정 지문

결과 `config.llm`에 `provider`, `reasoning_effort`, `temperature_sent`, `tool_strict`, `reasoning_items_roundtrip`,
`accepted_response_models`, 세 단가 값, `price_source`가 들어간다(`llm_config_info`가 단일 출처).
OpenAI는 스냅샷 ID를 공개하지 않으므로 응답의 실제 모델은 에피소드 결과의 모델 필드에 기록되고, 응답 모델이
설정과 다르면 `ModelMismatchError`로 기록된다. 응답 모델에 날짜 접미가 붙으면(`gpt-6-luna-2026-11-01`) 어댑터와
러너 모두 같은 모델로 보고(불일치 아님), 실제 응답 모델은 그대로 기록한다. `stats` 출력의 `response_models`는
실행 전체의 응답 모델 집합이고, 둘 이상이면 경고한다. 재현성이 ADR-0009 기준보다 약하다는 점을
파일럿 결과에 명시한다.

## 알려진 한계

- 평가 경로(`assistant_v0`, `scripted_tool`)는 effort가 `none`이 아닐 때 암호화된 추론 항목
  (`include=["reasoning.encrypted_content"]`)을 요청하고, 다음 요청에 함수 호출 앞 순서대로 되돌려 보낸다
  (`provider_items`). 이 항목이 품질에 주는 영향은 측정하지 않았다.
- 워커 경로(`runflow`)는 `provider_items`를 그래프 상태·DB에 저장하지 않고 버린다. 따라서 워커에서
  OpenAI 추론 모델을 쓰면 다음 단계 요청에 추론 항목이 없다. 파일럿(평가 경로)에는 해당하지 않는다.
- 도구 정의는 `strict: false`로 보낸다(기존 JSON Schema 유지). `strict: true`는 스키마 제약이 달라 쓰지 않았다.
- 추론 토큰이 `output_tokens`에 포함된다는 가정으로 비용을 계산한다. 첫 실행에서 `usage` 값을 확인한다.
- 실제 Responses API 응답 형태는 문서 기준 가정이다. 첫 사전 측정에서 확인한다.
