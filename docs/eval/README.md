# 평가 결과 대장

평가 단계에서 실행한 모든 평가를 한곳에서 찾기 위한 목록이다. 수치의 정본은 각 행의 "상세" 문서이며, 이 문서는 그 요약과 위치만 담는다. 숫자를 고칠 때는 상세 문서를 먼저 고치고 이 표를 맞춘다.

## 읽는 순서

1. 평가 설계: [`docs/evaluation-plan-draft.md`](../evaluation-plan-draft.md), 기반 [ADR-0009](../adr/ADR-0009-evaluation-foundation-v0.md)·[ADR-0010](../adr/ADR-0010-structured-answer-scoring.md)
2. 평가셋 v1: [ADR-0026](../adr/ADR-0026-eval-suite-v1.md) (생성·검수·봉인), [ADR-0027](../adr/ADR-0027-all-day-calendar-timezone.md)
3. 실행 도구: [ADR-0023](../adr/ADR-0023-runflow-evaluation.md) (runflow 평가), [ADR-0028](../adr/ADR-0028-retrieval-eval-runner.md) (검색), [ADR-0029](../adr/ADR-0029-eval-metrics-v1.md) (지표·Judge), [ADR-0030](../adr/ADR-0030-agent-pattern-variants.md) (패턴), [ADR-0034](../adr/ADR-0034-evaluation-deadlines.md) (시간 상한)
4. 결과: [`results-v1.md`](results-v1.md), 기준 설정 결정 [ADR-0031](../adr/ADR-0031-base-config-v1.md)

## 평가 목록

비교 기준을 꼭 함께 읽는다. dev 비교의 기준은 `luna · react · 색인 끔`이고, ADR-0031 이후 기준 설정은 `luna · react · 색인 켬`이다. 괄호 안 차이는 기준 대비 짝 차이이며 CI는 cluster bootstrap 95%다.

| ID | 날짜 | 평가 | 규모 | 핵심 결과 | 결정·상태 | 상세 | 원본 (`runs/` 기준) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| E00 | 2026-11-15 | v0 dev 기준 비교와 원인 ablation (`facts-v1`, `reasoning-rt`) | v0 dev 36문항 × 3회 | 수치를 문서 표로 남기지 않았다 (출처 확인 필요) | v1 평가셋으로 대체 | ADR-0023 §10 | `eval-1/`, `eval-2/`, `ablation-1/` |
| D01 | 2026-11-16 | 평가셋 v1 생성·자동 검수·사람 검수·봉인 | 과제 240 → 218 (dev 88 / test 130), 검색 질의 180 → 133 (dev 55 / test 78) | 본문 7,022개를 LLM 3계열이 작성($1.75). LLM Judge 자동 검수 372항목 중 54항목 표시($0.33). 사람 검수 제외 과제 22, 검색 47. 표현 변형 검수 문제 0건 | 봉인 완료 (test 130문항·102묶음) | ADR-0026 §6·§8, results-v1 "평가셋 구축에 쓴 LLM" | `eval/datasets/orbit-eval/v1/` (저장소) |
| R01 | 2026-11-17 | 검색 방식 비교 (메일만) | test 51질의·19묶음, 메일함 200/1,000/5,000 | 5,000통 R@10: vector 0.71, hybrid 0.69, bm25 0.67, keyword 0.47, gmail_search 0.14 | 방식 재선정 안 함 (test 선택 편향) | results-v1 "검색 세트 test 결과" | `retrieval/test-openai-v1.json`. dev는 `retrieval/dev-openai-v1.json` (dev 수치는 문서화 안 됨) |
| B01 | 2026-11-17 | dev 기준선 | 88과제 × 3회 | luna · react · 색인 끔 0.49 [0.39, 0.59], assistant_v0 0.42 | 비교 기준 | results-v1 "dev 설정 비교" | `baseline-v1/` |
| M01 | 2026-11-17 | dev 모델 비교 | 88 × 3 | haiku 0.67 (+0.186), gemini-3.5-flash-lite 0.39 (−0.102, 답할 수 없는 질문 환각률 0.24) | haiku는 비용 약 6배 | results-v1 "dev 설정 비교" | `models-v1/` |
| A01 | 2026-11-17 | dev 에이전트 패턴 비교 | 88 × 3 | fixed_workflow 0.55 (+0.061, CI 0 포함), plan_execute 0.39 (−0.098) | react 유지 | results-v1, ADR-0030 | `compare-v1/pattern-*.jsonl` |
| G01 | 2026-11-17 | dev 메일 색인 검색(RAG) 켬 | 88 × 3 | 0.64 (+0.155 [+0.072, +0.244]) | **채택** (ADR-0031) | results-v1, ADR-0031 | `compare-v1/index-on.jsonl` |
| G02 | 2026-11-17 | dev 추론 강도 low vs none (색인 켬) | 88 × 3 | low 0.602, low − none −0.042 [−0.109, +0.020] | none 유지, 우열 미확정 | results-v1 "추론 강도 low 비교" | `compare-v1/index-on-low.jsonl`, `followup-20261117/` |
| T01 | 2026-11-17 | 봉인 test 확인 | 130과제 × 1회, 102묶음 | react 0.454, 색인 켬 0.646 (+0.192), haiku 0.577 (+0.123), plan 0.392, gemini 0.446, assistant 0.400 | dev 방향 재확인 | results-v1 "종단 test 확인" | `test-v1/` |
| T02 | 2026-11-17 | fixed_workflow 정지 수정 후 test 재확인 | 130 × 1 | 0.492 (+0.038, CI 0 포함), 130/130 완주 | 정지 원인 미확정 | results-v1, ADR-0034 | `test-v1/luna-fixed.jsonl`, 중단 기록 `crashed/` |
| J01 | 2026-11-17 | Judge 간 일치도 (저가 2계열) | 기준선 528에피소드 | kappa 0.10 [0.003, 0.203] | 기준 미달 | results-v1 "Judge 근거성 비교" | `judge/agreement-ab.json` |
| J02 | 2026-11-17 | Judge–사람 일치 (judge-v1, 사람 60건) | 60 | 일치율 haiku 0.70, lite 0.85, sonnet 0.82, gemini-3.6-flash 0.92. 사람 라벨이 거의 모두 근거 있음이라 탐지 능력은 입증하지 못함 | 과거 기록 | results-v1 | `judge/agreement-human.json`, `judge/agreement-strong60.json`, `judge/human-labels.jsonl` |
| J03 | 2026-11-17 | Judge 변형 탐지 v2 (표면 치환 30 + 원본 30) | 60 | 3종 모두 재현율 0.86 이상. 문자열 대조로도 잡히는 변형이라 Judge 고유 가치 측정 불가 | 한계 기록 후 v3로 대체 | results-v1 "v2 세트" | `judge/blind60-*`, `judge/v2-blind60-*/` |
| J04 | 2026-11-17 | Judge 변형 탐지 v3 (의미 변형 24 + 표면 6 + 원본 30) + 사람 라벨 | 60 | surface 외 탐지: 코드 대조 1/24, Flash 24/24, Haiku 24/24, Lite 18/24. 사람 kappa: Flash 0.636 [0.446, 0.805], Haiku 0.575, Lite 0.454 | **Judge = gemini-3.6-flash 채택**, 보조 지표로 보고 | results-v1 "v3 세트", ADR-0029 §3 | `judge/v3-blind60-set/`, `judge/v3-blind60-*/` |

## 아직 측정하지 않은 것

- 검수 자기 일치도 재검수(약 10%): 진행을 막지 않는 후속 작업 (ADR-0026 §8)
- 남은 에피소드 전체의 Judge 근거성 채점(약 1,300건, Flash 기준 예상 약 $4.4)과 기능 부재 단정 규칙을 반영한 프롬프트 버전 갱신
- haiku + 색인 조합, low의 test 확인
- 맥락 구성 실험과 안전·가드레일 세트: 초안 ADR-0032·0033 (PR #37, 미실행)

## 원본 보관

- 실행 원본(`runs/`)은 합성 메일·문서 본문과 개별 판정을 담고 있어 git에서 제외한다. 평가 worktree가 정리돼도 남도록 저장소 밖 로컬 보관소 `../orbit-eval-archive/v1/runs/`(메인 체크아웃 기준 상대 경로)에 복사했다. 임베딩 캐시(`embed-cache/`)는 다시 만들 수 있어 제외했다.
- 보관 시점(2026-11-17) 파일 164개의 SHA-256 목록은 `../orbit-eval-archive/v1/SHA256SUMS`에 있다. 그 목록 파일 자체의 SHA-256은 `0359757e5a7866828d042f0213676d106c2db5c2870540835d38a824f7a6972a`다.
- 개별 결과 파일의 설정 지문과 SHA-256은 `results-v1.md`의 "수치 출처와 재현 정보"와 "test 출처"에 있다.

## 새 평가를 추가할 때

1. 실행 결과를 `runs/<이름>/`에 남기고, 끝나면 보관소에 복사한 뒤 `SHA256SUMS`를 갱신한다.
2. 수치와 조건(코드 SHA, 설정 지문, 비용, 짝 누락, 한계)을 `results-v1.md` 또는 새 결과 문서에 먼저 쓴다.
3. 이 표에 한 행을 추가한다. 측정하지 않은 값은 비우지 말고 "출처 확인 필요" 또는 "미측정"으로 적는다.
4. 결정이 생기면 ADR에 기록하고 "결정·상태" 칸에 링크한다.
