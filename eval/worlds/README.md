# 평가 세계 모델

- `orbit-v0.toml`: 평가 데이터 v0(템플릿 문장) 세계 모델. `python -m orbit.evaldata.generate`가 읽는다.
- `orbit-v1.toml`: Sim 세계 v1(ADR-0026) 세계 모델. 합성 데이터이며 주소는 `example.*`·`.test` 도메인뿐이다.

## v1 종단 과제·스모크 생성기

저장소 루트에서 실행한다. 기존 `world.json`·`skeleton.json`은 읽기만 한다.

```bash
# LLM 호출 없이 원본 192 + 장애 파생 24문항을 생성한다.
uv run --frozen --directory backend python -m orbit.evaldata.v1.tasks \
  --dataset-dir eval/datasets/orbit-eval/v1 --seed 20261116 --skip-paraphrase
# 표현 변형 24문항에 필요한 비용만 추정한다. 키를 읽거나 LLM을 만들지 않는다.
uv run --frozen --directory backend python -m orbit.evaldata.v1.tasks \
  --dataset-dir eval/datasets/orbit-eval/v1 --seed 20261116 --max-cost-usd 0.1 --dry-run
```

- 출력: `tasks.jsonl`, `manifest.json`. `--out /tmp/orbit-tasks-v1`로 별도 출력할 수 있다.
  별도 출력에는 로드용 `world.json`도 복사한다. `review.jsonl`은 만들지 않는다.
- 같은 입력·시드·생성기 SHA·표현 변형 캐시면 바이트 단위로 같다.
  원본의 정답 지문은 집합 순서·UTC 순간을 정규화하며, 기권·승인 거절은 대상도 구분한다.
  장애·표현 변형은 같은 정답을 의도한 파생이므로 원본과 함께 유지한다.
- 근거와 시나리오 연결 요소로 묶어 dev/test를 분할한다. test 묶음 100개 미만이면 실패한다.
  표현 변형을 생략하면 216문항이고 7:12의 test 비중을 유지한다. 변형을 포함하면 240문항이다.
- 스모크는 dev의 25문항이며 목록은 `manifest.smoke_task_ids`에 저장한다.
  러너의 `run --subset smoke --split dev`로 그 목록만 실행한다.
  `--subset smoke --split test`는 봉인 확인 플래그가 있어도 거부한다.
  러너에 실제 모델 호출 옵션을 주지 않으면 스크립트 LLM만 사용한다.
- 표현 변형 실제 생성은 별도 비용 승인·키 설정 후 사람이 실행한다.
  `--max-cost-usd`는 저장된 모든 시도의 비용을 포함한 누적 상한이다.
  이미 진행 중인 동시 호출 비용만큼 초과할 수 있어, 전체 예산보다 여유 있게 낮게 둔다.
  `paraphrase.jsonl`로 재개하며, 검증 통과 문장과 시도·모델 설정·비용만 저장한다.
  모델·경로·단가 설정이 바뀌면 기록 혼합을 거부한다.
  단가 기본값은 예시이므로 실제 실행 전 공급자 가격과 모델 사용 가능 여부를 확인한다.
- 답장 쓰기는 제품에 초안 저장 전용 도구가 없어, 승인 후 발송 요청과 `draft_sent`로 채점한다.
  모든 실행은 Sim의 합성 데이터에만 적용한다. 초안만 저장하는 제품 기능은 이번 범위 밖이다.
- 사람 검수·test 봉인은 다음 단계다. 질문의 답 종류 유일성, 최신 값과 근거 메일의 타당성,
  기권 대상의 부재, 두 모호성 해석의 자연스러움, 시간대·상대 날짜를 우선 확인한다.

## v1 구조 생성기

검색용 질의·관련도 데이터 생성은 아래 "v1 검색 세트"를 참고한다.

```bash
cd backend
uv run python -m orbit.evaldata.v1.build --world ../eval/worlds/orbit-v1.toml \
  --seed 20261116 --out /tmp/orbit-world-v1 --template-world
```

- `skeleton.json`: 사람·일정·메일 스레드·문서와 시나리오 정답 재료. 본문은 비어 있고 본문 명세(`bodies`)만 있다.
- `skeleton-stats.json`: 계정별 수량, 계열별 본문 수·글자 수, 시나리오 분포(본문 실현 비용 추정 근거).
- `world.template.json`(`--template-world`): 템플릿 본문으로 채운 Sim 세계 스냅샷.
- 같은 세계 모델·시드·`--git-sha`면 `skeleton.json`이 바이트 단위로 같다. LLM과 네트워크를 쓰지 않는다.
- 종일 일정은 ADR-0027 해석대로 그 캘린더 시간대의 00:00~다음 날 00:00인 순간이다(일광 절약 시간 전환일은 24시간이 아닐 수 있다). `all_day` 시나리오 정답에는 더 이상 한계 표시(`contract_caveat`)를 쓰지 않는다. 빈 날을 고르는 배치 규칙은 이미 만든 본문을 재사용하려고 UTC 경계 해석일 때와 같게 둔다(`core.py`의 `all_day_footprint`).
- 산출물은 저장소 밖 임시 위치에 쓴다. 본문 실현(LLM)과 검수를 거친 `world.json`만 커밋 대상이다.

## v1 본문 실현기 (LLM)

구조 생성기가 만든 `skeleton.json`의 본문 명세마다 계열별 모델이 본문을 한 번 쓰고, 코드가 검증한 본문만 저장한다.
API 키는 환경 변수 `ORBIT_OPENAI_API_KEY`, `ORBIT_OPENROUTER_API_KEY`로 읽는다.

```bash
cd backend
# 1) 호출 없이 비용만 추정
uv run python -m orbit.evaldata.v1.realize --skeleton /tmp/orbit-world-v1 --out /tmp/orbit-world-v1-bodies \
  --max-cost-usd 1 --dry-run
# 2) 시험 생성: 계열당 30건(총 90건)을 층화 표본으로
uv run python -m orbit.evaldata.v1.realize --skeleton /tmp/orbit-world-v1 --out /tmp/orbit-world-v1-bodies \
  --max-cost-usd 0.5 --limit 90 --sample-seed 1
# 3) 전체 생성과 조립(같은 --out이면 이미 만든 본문은 건너뛰고 이어서 쓴다)
uv run python -m orbit.evaldata.v1.realize --skeleton /tmp/orbit-world-v1 --out /tmp/orbit-world-v1-bodies \
  --max-cost-usd 5 --assemble
```

- `--models-config <toml>`로 계열별 경로·모델 ID·단가를 정한다(형식은 `realize_models.py` 상단 설명). `--openai-route openrouter`는 OpenAI 직접 크레딧이 바닥났을 때 쓴다.
- `bodies.jsonl`(본문과 시도·토큰·비용 기록), `realize-manifest.json`(모델·단가·집계), `world.json`(`--assemble`)이 출력 디렉터리에 생긴다. 실패한 시도의 응답 원문은 저장하지 않는다.
- 종료 코드: 0 정상, 2 입력·설정 오류, 3 비용 상한 도달(이어서 실행 가능), 4 영구 오류로 중단, 5 조립 불가(`--allow-template-missing`으로 빠진 본문을 템플릿으로 채울 수 있다).
- `--recheck` 실행 후에는 매니페스트 `prompt_version`이 새 버전으로 바뀌어 일반 이어쓰기의 버전 혼재 거부가 해제된다. recheck 전에 반드시 `--dry-run`으로 실패 건수를 확인한다.
- 생성기를 고쳐 skeleton이 바뀌었지만 `bodies` 구역(본문 명세)이 같으면, 이미 만든 `bodies.jsonl`로 `world.json`만 다시 조립할 수 있다(LLM 호출 없음). 새 skeleton을 `--skeleton`에 주고 기존 `bodies.jsonl`·`realize-manifest.json`이 있는 디렉터리를 `--out`으로 준 뒤 `--assemble --allow-skeleton-change`를 붙인다. 매니페스트에 `bodies_sha256`이 없으면 `--previous-skeleton <이전 skeleton.json>`도 준다. 본문 명세가 하나라도 다르면 다른 body_id 수를 출력하고 거부한다. 매니페스트에 이전·새 skeleton sha와 bodies 해시가 남는다.

## v1 검색 세트

저장소 루트에서 실행한다. 실제 LLM·임베딩·검색 API는 호출하지 않는다.

```bash
uv run --frozen --directory backend python -m orbit.evaldata.v1.search_set \
  --dataset-dir eval/datasets/orbit-eval/v1 --seed 20261116
# 검토용 임시 출력
uv run --frozen --directory backend python -m orbit.evaldata.v1.search_set \
  --dataset-dir eval/datasets/orbit-eval/v1 --seed 20261116 --out /tmp/orbit-search-v1
```

- 출력은 `search.jsonl`, `search-manifest.json`이다. 기존 `world.json`·`skeleton.json`·`tasks.jsonl`은 읽기만 한다.
- `query_id` 하나가 메일함 세 크기에 공통으로 쓰인다. 각 행의 `mailboxes`는 크기 문자열 `200`·`1000`·`5000`별 `account_id`와 `relevance`(해당 계정의 메시지/파일 ID → 등급)를 가진다.
- 질의는 키워드·질문·사람/시점 형태를 섞는다. 정답 금액·날짜의 표기를 질의에 넣지 않으며, 주제 문구가 다른 주제와 겹치면 버린 사유를 기록한다.
- 필터·중복 제거 후 질의가 2개 미만 남는 주제는 남은 질의도 버리고 `insufficient_forms`로 기록한다. 실제 입력은 모든 주제에서 세 형태가 유지된다.
- 관련도 2는 정답 근거, 1은 이전 값 등 부분 관련, 0은 명시된 오답 후보다. 작은 메일함에 없는 등급 0 후보는 그 메일함의 맵에서 생략한다. 등급 1·2는 세 크기에 모두 있어야 한다.
- 부분집합과 근거 공유는 계정별로 다른 ID 대신 `body_id`로 검사한다. 공유 body를 가진 주제와 같은 주제의 질의는 한 묶음으로 합쳐 dev/test로 나눈다.
- test 목표는 d2와 같은 전체 질의의 7/12이며, 오차는 `max(3개, 전체의 5%)` 이내여야 한다. 묶음을 보존하면서 이 조건과 두 분할의 비어 있지 않음을 만족하지 못하면 생성에 실패한다.
- `load_search_set(search_jsonl, manifest_path)`로 저장 파일의 형식·중복·공유 근거 연결 요소와 분할, 매니페스트 수량을 확인할 수 있다. 원본 세계의 ID·본문 검사는 생성 단계에서 수행한다.
- 검색 지표 함수는 `orbit.evalrunner.retrieval_metrics`의 `recall_at_k`(등급 2 또는 1 이상), `mrr`(첫 등급 2), `ndcg_at_k`(기본 10, 선형 gain)다. 결과 ID의 중복은 첫 등장만 남긴 뒤 순위를 계산하며, 관련 항목이 없으면 0을 반환한다.
- 실제 검색 실행기와 색인 저장·재사용은 이번 단계에 없다. 사람 검수·test 봉인 후 RAG 비교 실험 전에 별도로 정한다. 변형 질의는 독립 표본이 아니므로 정밀도·bootstrap은 묶음 단위를 기준으로 해석해야 한다.
