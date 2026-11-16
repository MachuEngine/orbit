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

## 실사용 경로(`runflow_v0`) 실행 (ADR-0023)

`runflow_v0`는 평가 러너가 실사용 그래프(`orbit.runflow`)를 워커 없이 직접 구동하는 평가 대상이다. 에피소드마다
평가 DB에 워크스페이스를 만들고 끝나면 지우므로 전용 DB `orbit_eval`이 필요하다. `ORBIT_EVAL_DATABASE_URL`
(앱 역할 `orbit_app`)이 없으면 실행하지 않고, 개발 DB(`orbit`)를 가리키면 거부한다.

### 평가 DB 준비

새 볼륨에서는 `backend/db/init/00_roles.sql`이 `orbit_eval`을 만든다. 기존 볼륨(이미 떠 있는 개발 DB)에는
`orbit_test`처럼 수동으로 만든다. 저장소 루트에서 실행한다.

```bash
docker compose -f backend/compose.yml exec -T postgres psql -U postgres -d postgres \
  -c "CREATE DATABASE orbit_eval OWNER orbit_owner"
docker compose -f backend/compose.yml exec -T postgres psql -U postgres -d orbit_eval <<'SQL'
GRANT CREATE, USAGE ON SCHEMA public TO orbit_owner;
GRANT USAGE ON SCHEMA public TO orbit_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE EXTENSION IF NOT EXISTS vector;
SQL

set -a; source backend/dev.env; set +a
ORBIT_MIGRATION_DATABASE_URL="$ORBIT_EVAL_MIGRATION_DATABASE_URL" \
  uv run --directory backend alembic upgrade head
```

스키마는 개발 DB와 같은 Alembic 마이그레이션으로 맞춘다. 마지막 명령의 `ORBIT_MIGRATION_DATABASE_URL`은 이 명령에만
평가 DB 값으로 덮어쓴다(개발 DB는 건드리지 않는다).

### 실행

```bash
set -a; source backend/dev.env; set +a
cd backend
uv run python -m orbit.evalrunner run \
  --dataset ../eval/datasets/orbit-eval/v0 --split dev --sut runflow_v0 --index off \
  --live-llm --estimate 5 --repeats 3 --seed 0 --prompt-version runflow_v0 \
  --max-cost-usd 0.5 --max-total-tokens 2000000 \
  --out ../runs/pilot/runflow-A.jsonl
```

- LLM 환경(`ORBIT_LLM_*`)은 위 `assistant_v0` 실행과 같다. 비용이 드는 실행은 사전 측정 뒤 개발자 승인을 받는다.
- `--index off`(기본)는 색인 의존성 없이 실행한다(`search_indexed_mail`은 `index_unavailable`). `--index fake`는
  아직 지원하지 않는다.
- 결과 행에는 `assistant_v0` 행의 필드에 더해 `approval_violations`(DB `execution`·`approval`과 Sim 호출 기록으로
  러너가 계산), `turns`(턴별 run 상태·승인 수·오류 종류), `reminders_created`가 있다. 설정 지문의
  `config.sut_config`에 runflow 설정 지문, 답 제출 확장 버전(`eval-answer-v2`), 색인 모드가 들어간다.
- 원인 확인용 변형(ADR-0023 §10): `--runflow-variant facts`(프롬프트에 `assistant_v0`의 [사실 확인] 단락을 더함,
  `facts-v1`)와 `--runflow-variant reasoning-rt`(추론 항목을 메모리에만 두고 다음 step 요청에 되돌림)를 켠다.
  옵션을 반복해 함께 켤 수 있고, `--sut runflow_v0`가 아니면 거부한다. 켠 변형은 SUT 버전 문자열 끝과
  `config.sut_config.variants`, runflow 설정 지문에 기록되며, 끄면 지문이 변형이 없던 때와 같다.
  예: `... --sut runflow_v0 --runflow-variant facts --runflow-variant reasoning-rt ...`
- 과제의 `approval_policy`(선택, 쓰기 순번별 `approve`·`reject`)는 시뮬레이션 사용자의 결정이다. 없으면 모두
  승인한다.
- 모델이 받는 도구 목록과 프롬프트에 `submit_answer`와 안내 한 단락이 더해진다. 실사용 입력과 정확히 같지
  않다(ADR-0023 타당성 위협).

## 평가 스위트 v1 측정 도구 (ADR-0026 §4·§7)

- **답 제출 안내 `eval-answer-v2`**: `assistant_v0`(프롬프트 `assistant-v0.4`)와 `runflow_v0`(`runflow-v0.8` +
  확장)가 같은 안내 문구(`orbit.assistant.answer_guide.answer_guide_lines`)를 받는다. 정답 유형마다 쓰는 상황과
  예시 질문 한 줄이 있다. v0 결과와 한 비교에 섞지 않는다.
- **새 정답 유형**: `message_set`(메일 집합), `document`, `abstain`(답할 정보 없음), `clarify`(되묻기, 모호한 질문 문항의
  정답이며 `readings`에 해석별 정답이 둘 이상 있다).
- **새 실패 유형**: `answer_kind`(답 종류만 틀리고 가리킨 일정·메일·문서 ID 집합은 정답과 같음), `ambiguity_ignored`
  (모호한 질문에 되묻지 않고 답함), `unneeded_clarify`(한 가지로 읽히는 질문에서 되물음). 종류도 대상도 틀리면
  `wrong_answer`다. 우선순위는 `answer_format` > `answer_kind` > `ambiguity_ignored` = `unneeded_clarify` > `wrong_answer`.
  성공 판정은 바뀌지 않는다(종류가 틀리면 실패).
- **결과 행 필드**: `calls`(커넥터 호출마다 `tool`, `args`, `outcome`, `error_type`. `args`는 요약본이라 본문 필드와
  80자 넘는 문자열은 `<str len=N>`이고 `result_summary`는 없다. 기존 `trajectory`는 유지), `expected_kind`(정답 종류),
  `matched_reading`(`ambiguity_ignored`일 때 제출이 일치한 해석 번호로 `readings` 인덱스 0부터, 없으면 null).
- **`stats` 출력**: 기존 키에 `answer_kind_rate`(채점된 에피소드 중 `answer_kind` 비율), `unneeded_clarify_rate`(정답이
  `clarify`가 아닌 채점된 에피소드 중 `unneeded_clarify` 비율), 정답이 `clarify`인 에피소드가 있으면
  `ambiguity_ignored_by_reading`(해석 번호별 개수, 일치 없음은 `"none"`)이 더해진다.
- **되묻기 지시**: 제품 프롬프트에 "질문이 두 가지 이상으로 읽히고 해석에 따라 답이 달라지면 되묻는다. 한 가지로 읽히면
  되묻지 않는다"는 줄이 들어갔다(`assistant-v0.4`, `runflow-v0.8`). 평가 전용 안내가 아니라 실사용 프롬프트다.
  `assistant_v0`에는 문서 도구가 없어 `document` 답은 낼 수 없다.

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
