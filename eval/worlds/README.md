# 평가 세계 모델

- `orbit-v0.toml`: 평가 데이터 v0(템플릿 문장) 세계 모델. `python -m orbit.evaldata.generate`가 읽는다.
- `orbit-v1.toml`: Sim 세계 v1(ADR-0026) 세계 모델. 합성 데이터이며 주소는 `example.*`·`.test` 도메인뿐이다.

## v1 구조 생성기

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
