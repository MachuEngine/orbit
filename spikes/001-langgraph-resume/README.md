# SPIKE-001: LangGraph interrupt 재개 실험

스파이크 코드다. 제품 코드로 쓰지 않는다. 실험 설계와 판정 기준은 `docs/spikes/SPIKE-001-langgraph-interrupt-resume.md`를 따른다.

## 실행

```bash
cd spikes/001-langgraph-resume
docker compose up -d --wait
uv sync
uv run python run_scenarios.py              # 검증 시나리오가 기대와 다르면 종료 코드 1
uv run python run_scenarios.py --repeat 5   # 5회 반복, 시나리오별 관측값 분포 요약
docker compose down
```

DB 비밀번호는 `ORBIT_SPIKE_PG_PASSWORD`(기본 `spike`)로 바꾼다. 볼륨이 없어 `down` 하면 DB가 사라지므로 실행마다 깨끗한 DB에서 시작한다.

## 파일

- `compose.yml`: PostgreSQL(`pgvector/pgvector:0.8.0-pg17`, 호스트 포트 55432)
- `graph.py`: `plan -> approval -> execute | rejected` 그래프와 부수 효과 테이블 `spike_effects`
- `worker.py`: `start`, `pending`, `resume`, `continue`, `status` 명령. 한 명령이 한 프로세스이며 마지막 줄에 `RESULT <json>`을 출력한다.
- `run_scenarios.py`: S1~S7을 별도 프로세스로 실행하고 관측표를 출력한다. 결과는 `results.json`에 회차별 원자료로 저장한다. `--repeat N`(기본 1)으로 반복하며, S4는 전제(start 성공, marker 관측, kill exit=-9)가 어긋나면 INVALID로 표시한다.

`results.json`은 실행 결과물이며 커밋 대상이 아니다.
