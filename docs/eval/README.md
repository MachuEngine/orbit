# 평가 결과 대장

## 메일 RAG 검색 정책 비교 (ADR-0035)

생성 모델 Luna·추론 강도 none·에이전트 ReAct를 고정하고 도구 호출형 RAG, Retrieve-once,
질의 재작성 후 Retrieve-once를 dev 메일 21과제와 별도 스모크에서 비교했다.
성공률은 76.2% / 74.6% / 81.0%이며 짝 차이 CI는 모두 0을 포함해 기준을 유지한다.
사전 추정 포함 LLM+임베딩 기록 비용은 $0.22854392, 비용 미상 0건이다.
설정·지표·실행 상태·비용과 출처는 [메일 RAG 검색 정책 비교 v1](rag-patterns-v1.md)을 따른다.
기존 종단 dev/test 전체 점수와 이번 부분집합 점수를 직접 순위 비교하지 않는다.

평가 단계에서 실행한 모든 평가를 한곳에서 찾기 위한 목록이다. 수치의 정본은 각 행의 "상세" 문서이며, 이 문서는 그 요약과 위치만 담는다. 숫자를 고칠 때는 상세 문서를 먼저 고치고 이 표를 맞춘다.

## 읽는 순서

1. 평가 설계: [`docs/evaluation-plan-draft.md`](../evaluation-plan-draft.md), 기반 [ADR-0009](../adr/ADR-0009-evaluation-foundation-v0.md)·[ADR-0010](../adr/ADR-0010-structured-answer-scoring.md)
2. 평가셋 v1: [ADR-0026](../adr/ADR-0026-eval-suite-v1.md) (생성·검수·봉인), [ADR-0027](../adr/ADR-0027-all-day-calendar-timezone.md)
3. 실행 도구: [ADR-0023](../adr/ADR-0023-runflow-evaluation.md) (runflow 평가), [ADR-0028](../adr/ADR-0028-retrieval-eval-runner.md) (검색), [ADR-0029](../adr/ADR-0029-eval-metrics-v1.md) (지표·Judge), [ADR-0030](../adr/ADR-0030-agent-pattern-variants.md) (패턴), [ADR-0034](../adr/ADR-0034-evaluation-deadlines.md) (시간 상한)
4. 결과: [`results-v1.md`](results-v1.md), 기준 설정 결정 [ADR-0031](../adr/ADR-0031-base-config-v1.md)

## 평가 목록

비교 기준을 꼭 함께 읽는다. dev 비교의 기준은 `luna · react · 색인 끔`이고, ADR-0031 이후 기준 설정은 `luna · react · 색인 켬`이다. 괄호 안 차이는 기준 대비 짝 차이이며 CI는 cluster bootstrap 95%다.

| ID | 날짜 | 평가 | 규모 | 핵심 결과 | 결정·상태 | 상세 | 원본 (`runs/` 기준) | 설계·선정 기록 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| P01 | 2026-11-11 | 모델 파일럿 | v0 dev 36×3·6묶음 | luna none/low 74.1/75.9%, terra low 100% | 실험 luna low, 실사용 terra→luna로 변경 | docs/pilot/results-2026-11-11.md, ADR-0015 | MAIN_RUNS/pilot/ | [P01](experiments.md#p01) |
| RG01 | 2026-11-12 | 7단계 정리 뒤 회귀 | v0 dev 36×1 | 72.2% | 회귀 관찰 | docs/implementation-plan.md §7, experiments.md | MAIN_RUNS/regress/stage7-luna-low.jsonl | [RG01](experiments.md#rg01) |
| RG02 | 2026-11-12 | 데이터 봉투 회귀 | v0 dev 36×1, 전후 2회 | 88.9%→77.8%, dirty 코드·동일 지문 | 인과 효과 확정 안 함 | ADR-0018, experiments.md | MAIN_RUNS/regress/sep*.jsonl | [RG02](experiments.md#rg02) |
| RG03 | 2026-11-12 | 인젝션 선행 동작 확인 | 6행×3실행 | 쓰기 호출·지침 문장 노출 0, 독립 ASR 아님 | 안전 세트 평가와 구분 | ADR-0018, experiments.md | MAIN_RUNS/regress/injection-*.jsonl | [RG03](experiments.md#rg03) |
| E00 | 2026-11-15 | v0 dev 기준 비교와 원인 ablation (`facts-v1`, `reasoning-rt`) | v0 dev 36문항 × 3회 | 1차 runflow−assistant −22.2%p, 2차 −11.1%p. ablation 개선 불확정 (파일럿 문서에 상세표 있음) | v1 평가셋으로 대체 | docs/pilot의 runflow 비교·ablation 결과, ADR-0023 §10 | `eval-1/`, `eval-2/`, `ablation-1/` | [P02](experiments.md#p02)·[P03](experiments.md#p03) |
| D01 | 2026-11-16 | 평가셋 v1 생성·자동 검수·사람 검수·봉인 | 과제 240 → 218 (dev 88 / test 130), 검색 질의 180 → 133 (dev 55 / test 78) | 본문 7,022개를 LLM 3계열이 작성($1.75). LLM Judge 자동 검수 372항목 중 54항목 표시($0.33). 사람 검수 제외 과제 22, 검색 47. 표현 변형 검수 문제 0건 | 봉인 완료 (test 130문항·102묶음) | ADR-0026 §6·§8, results-v1 "평가셋 구축에 쓴 LLM" | `eval/datasets/orbit-eval/v1/` (저장소) | [D01](experiments.md#d01) |
| R01 | 2026-11-17 | 검색 방식 비교 (메일만) | dev 37질의·13묶음 / test 51·19, 메일함 200/1,000/5,000 | 5,000통 R@10: vector 0.71, hybrid 0.69, bm25 0.67, keyword 0.47, gmail_search 0.14 | 방식 재선정 안 함 (test 선택 편향) | results-v1 "검색 세트 test 결과" | `retrieval/test-openai-v1.json`. dev는 `retrieval/dev-openai-v1.json` (이번에 dev 표 추가) | [R01](experiments.md#r01) |
| R02 | 2026-11-19 | 메일 RAG 검색 정책·질의 재작성 비교 | dev 메일 21과제·17묶음 × 3, 별도 스모크 21 × 1 | R0 76.2%, R1 74.6%, R2 81.0%; R2−R0 +4.8 pp [−3.3, +12.3] | 기준 유지·개선 미확정, test 미실행 | [rag-patterns-v1](rag-patterns-v1.md), ADR-0035 | 별도 worktree의 `runs/rag-patterns-v1/`  | [R02](rag-patterns-v1.md) |
| R03 | 2026-11-22 | 메일 RAG 아키텍처 비교 (Naive·Advanced·Agentic·Adaptive·GraphRAG) | rag-v1 dev 64 / test 96문항·24묶음 × 3 | test 성공률 A1 0.601, A2 0.483; A1−A2 +0.118 [0.052, 0.184]. 정답 비교 Judge 일치율 0.942 (단일 조회 0.838) | 기준 유지, 불일치 29건 Claude 원문 대조: Judge 27·코드 2 (사람 판정 아님) | [rag-architecture-v1](rag-architecture-v1.md), ADR-0036 | `eval/runs/rag-arch-v1/` | [R03](rag-architecture-v1.md) |
| R03b | 2026-11-22 | GraphRAG local 메일 순위에 reranker 적용 (GraphRAG + reranker, 그래프 없는 대조군) | rag-v1 dev 64 / test 96문항·24묶음 × 3 | test 성공률 A4R 0.625, A4RN 0.590; A4R−A4 +0.257 [0.132, 0.389], A4R−A2 +0.142 [0.063, 0.219], A4R−A1 +0.024 [−0.052, 0.108], A4R−A4RN +0.035 [−0.010, 0.080]. 정답 비교 Judge 일치율 0.915 (단일 조회 0.746) | 기준 유지, 그래프 기여 미확인, 단일 조회 불일치 17건 Claude 원문 대조: 모두 Judge (사람 판정 아님) | [rag-architecture-v1](rag-architecture-v1.md#r03b-graphrag-local-순위에-reranker-적용), ADR-0037 | `eval/runs/rag-arch-v1/` | [R03b](rag-architecture-v1.md#r03b-graphrag-local-순위에-reranker-적용) |
| R03c | 2026-11-23 | Agentic RAG 검색 도구에 Advanced RAG 검색 단계 넣기 (Agentic RAG + reranker, Agentic RAG + Advanced 검색) | rag-v1 dev 64 / test 96문항·24묶음 × 3 | test 성공률 A2X 0.663, A2R 0.500; A2X−A2 +0.181 [0.108, 0.253], A2X−A1 +0.062 [0.007, 0.118], A2X−A4R +0.038 [−0.024, 0.101], A2R−A2 +0.017 [−0.049, 0.087]. 정답 비교 Judge 일치율 0.895 (단일 조회 0.682) | 기준 유지(평가 경로 전용), 요소 분리 미확인, 불일치 21건 Claude 원문 대조: Judge 19·코드 2 (사람 판정 아님) | [rag-architecture-v1](rag-architecture-v1.md#r03c-agentic-rag-검색-도구에-advanced-rag-검색-단계-넣기), ADR-0038 | `eval/runs/rag-arch-v1/` | [R03c](rag-architecture-v1.md#r03c-agentic-rag-검색-도구에-advanced-rag-검색-단계-넣기) |
| B01 | 2026-11-17 | dev 기준선 | 88과제 × 3회 | luna · react · 색인 끔 0.49 [0.39, 0.59], assistant_v0 0.42 | 비교 기준 | results-v1 "dev 설정 비교" | `baseline-v1/` | [B01](experiments.md#b01) |
| M01 | 2026-11-17 | dev 모델 비교 | 88 × 3 | haiku 0.67 (+0.186), gemini-3.5-flash-lite 0.39 (−0.102, 답할 수 없는 질문 환각률 0.24) | haiku는 비용 약 6배 | results-v1 "dev 설정 비교" | `models-v1/` | [M01](experiments.md#m01) |
| A01 | 2026-11-17 | dev 에이전트 패턴 비교 | 88 × 3 | fixed_workflow 0.55 (+0.061, CI 0 포함), plan_execute 0.39 (−0.098) | react 유지 | results-v1, ADR-0030 | `compare-v1/pattern-*.jsonl` | [A01](experiments.md#a01) |
| G01 | 2026-11-17 | dev 메일 색인 검색(RAG) 켬 | 88 × 3 | 0.64 (+0.155 [+0.072, +0.244]) | **채택** (ADR-0031) | results-v1, ADR-0031 | `compare-v1/index-on.jsonl` | [G01](experiments.md#g01) |
| G02 | 2026-11-17 | dev 추론 강도 low vs none (색인 켬) | 88 × 3 | low 0.602, low − none −0.042 [−0.109, +0.020] | none 유지, 우열 미확정 | results-v1 "추론 강도 low 비교" | `compare-v1/index-on-low.jsonl`, `followup-20261117/` | [G02](experiments.md#g02) |
| T01 | 2026-11-17 | 봉인 test 확인 | 130과제 × 1회, 102묶음 | react 0.454, 색인 켬 0.646 (+0.192), haiku 0.577 (+0.123), plan 0.392, gemini 0.446, assistant 0.400 | dev 방향 재확인 | results-v1 "종단 test 확인" | `test-v1/` | [T01](experiments.md#t01) |
| T02 | 2026-11-17 | fixed_workflow 정지 수정 후 test 재확인 | 130 × 1 | 0.492 (+0.038, CI 0 포함), 130/130 완주 | 정지 원인 미확정 | results-v1, ADR-0034 | `test-v1/luna-fixed.jsonl`, 중단 기록 `crashed/` | [T02](experiments.md#t02) |
| J01 | 2026-11-17 | Judge 간 일치도 (저가 2계열) | 기준선 528에피소드 | kappa 0.10 [0.003, 0.203] | 기준 미달 | results-v1 "Judge 근거성 비교" | `judge/agreement-ab.json` | [J01](experiments.md#j01) |
| J02 | 2026-11-17 | Judge–사람 일치 (judge-v1, 사람 60건) | 60 | 일치율 haiku 0.70, lite 0.85, sonnet 0.82, gemini-3.6-flash 0.92. 사람 라벨이 거의 모두 근거 있음이라 탐지 능력은 입증하지 못함 | 과거 기록 | results-v1 | `judge/agreement-human.json`, `judge/agreement-strong60.json`, `judge/human-labels.jsonl` | [J02](experiments.md#j02) |
| J03 | 2026-11-17 | Judge 변형 탐지 v2 (표면 치환 30 + 원본 30) | 60 | 3종 모두 재현율 0.86 이상. 문자열 대조로도 잡히는 변형이라 Judge 고유 가치 측정 불가 | 한계 기록 후 v3로 대체 | results-v1 "v2 세트" | `judge/blind60-*`, `judge/v2-blind60-*/` | [J03](experiments.md#j03) |
| J04 | 2026-11-17 | Judge 변형 탐지 v3 (의미 변형 24 + 표면 6 + 원본 30) + 사람 라벨 | 60 | surface 외 탐지: 코드 대조 1/24, Flash 24/24, Haiku 24/24, Lite 18/24. 사람 kappa: Flash 0.636 [0.446, 0.805], Haiku 0.575, Lite 0.454 | **Judge = gemini-3.6-flash 채택**, 보조 지표로 보고 | results-v1 "v3 세트", ADR-0029 §3 | `judge/v3-blind60-set/`, `judge/v3-blind60-*/` | [J04](experiments.md#j04) |
| M02 | 2026-11-18 | 색인 켬 후보 재비교 | dev 2후보×264, test 3후보×130, 사전 추정 10×8 | dev Haiku/fixed 0.617/0.670, test Haiku/fixed/plan 0.577/0.631/0.469. 기준을 유의하게 웃도는 후보 없음 | 기준 유지. Google dev/test·Plan dev 미실행. 관측 LLM ≥$2.270643545, 비용 미기록 16건 | [색인 켬 기준 재비교](results-v1.md#색인-켬-기준-재비교) | `compare-v2/`, `test-v1/*-index.jsonl`, `complete-v1/` | [M02](experiments.md#m02) |
| J05 | 2026-11-19 | test Judge-v3 입력 준비·비용 조건 확인 | 가용 10설정 1,300건→eligible 1,267 (답 없음 33 제외) | 전체 dry-run $6.121430 > 실행 조건 $5.50 | 실제 호출 0·지출 $0, 근거성·엄격 성공률 미측정. 추가 결정 필요 | [Judge-v3 준비 결과](results-v1.md#test-judge-v3) | `complete-v1/test-judge-final-input.jsonl`, `judge-execution-plan.json` | [J05](experiments.md#j05) |
| C01 | 2026-11-24 | Agent 코드 채점 문자열 검사 감사 (Jev 재채점, Gemini 2차 판정) | dev·test 20개 원본 2,450건 Jev v4, 목록 과제는 시각에 일정 제목을 붙여 재채점. 불일치·경계·표본 686건은 Gemini 재판정 | 사람 대조 62건 중 61건 일치. Gemini는 Jev와 685/686 일치(무작위 표본 100/100). test haiku-index−haiku +0.077 [+0.008, +0.146] (원래 0.000), haiku-index−luna-index +0.069 [−0.024, +0.164]; dev haiku-index−index-on +0.106 [+0.011, +0.204]. 패턴·Luna 색인 결론 유지 | Agent 주 점수를 코드 구조 검사 + Jev + Gemini 판정으로 변경. ADR-0031 재검토는 개발자 결정. 목록 과제 질문 형식은 사후 수정. 비용 $1.19 | [문자열 검사 오채점 감사](results-v1.md#문자열-검사-오채점-감사jev-재채점-2026-11-24) | `followup/grader-audit-20261124/` | [C01](experiments.md#c01) |
| C02 | 2026-11-24 | 구조화 답 종류 오채점 재판정 | `answer_kind` 실패 243건 중 대상 일정은 맞고 종류만 `event`로 표시한 230건. 기존 Jev 행 사용, 갈림·경계 75건은 Gemini 재판정 | 229건 성공으로 정정(Gemini=Jev 74/75). test luna-index 0.708, haiku-index 0.762. dev gemini−runflow 유의 차이 사라짐, haiku-index−index-on +0.080 [−0.008, +0.170]로 CI 0 포함. test 결론 유지 | 최종 주 점수로 반영. `event_set` 오답 13건은 실패 유지. 비용 $0.10 | [구조화 답 종류 오채점 재판정](results-v1.md#구조화-답-종류-오채점-재판정2026-11-24) | `followup/grader-audit-20261124/` | [C02](experiments.md#c02) |

중단·폐기 실행은 [I01](experiments.md#i01)·[I02](experiments.md#i02)·[I03](experiments.md#i03)에 원인·조치·영향·기록 비용을 분리했다. 새 비교의 완료·공백·비용 불확실성은 [M02](experiments.md#m02), Judge의 비용 조건 중단은 [J05](experiments.md#j05)에 기록했다.

## 아직 측정하지 않은 것

- 검수 자기 일치도 재검수(약 10%): 진행을 막지 않는 후속 작업 (ADR-0026 §8)
- dev 근거성은 미채점이다. test Judge-v3도 전체 추정 $6.121430이 실행 조건을 넘어 미실행이며, 재개에는 추가 예산 또는 범위에 관한 개발자 결정이 필요하다. 프롬프트 두 규칙은 구현했다.
- 문항 품질·문장 품질·유용성 Judge: 사람 검증을 거쳐야 하는 후속 작업
- low의 test 확인, 비용 조건 때문에 빠진 Google 색인 켬 dev/test와 Plan 색인 켬 dev
- 임베딩 모델·800자 청크·RRF k=60의 대안 비교
- RAG 아키텍처 비교(A0~A4): ADR-0036 채택, 구현 전
- 맥락 구성 실험과 안전·가드레일 세트: 초안 ADR-0032·0033 (PR #37, 미실행)

## 원본 보관

- 실행 원본(`runs/`)은 합성 메일·문서 본문과 개별 판정을 담고 있어 git에서 제외한다. 평가 worktree가 정리돼도 남도록 저장소 밖 로컬 보관소 `../orbit-eval-archive/v1/runs/`(메인 체크아웃 기준 상대 경로)에 복사했다. 임베딩 캐시(`embed-cache/`)는 다시 만들 수 있어 제외했다.
- 보관 시점(2026-11-19) 파일 289개의 SHA-256 목록은 `../orbit-eval-archive/v1/SHA256SUMS`에 있다. 그 목록 파일 자체의 SHA-256은 `c38d2a2ef1fb76fd2d2a63c24a1b8e0d7ab252b9c21993ac842e27302df44e06`다. 이번에 새 결과·추정·비용/상태·입력 준비·진단·재현 스크립트 125개를 추가했다. Judge 출력 경로에는 비용 조건 중단 상태만 보존했고 판정 파일은 생성하지 않았다.
- 개별 결과 파일의 설정 지문과 SHA-256은 `results-v1.md`의 "수치 출처와 재현 정보"와 "test 출처"에 있다.

## 새 평가를 추가할 때

1. 실행 결과를 `runs/<이름>/`에 남기고, 끝나면 보관소에 복사한 뒤 `SHA256SUMS`를 갱신한다.
2. 수치와 조건(코드 SHA, 설정 지문, 비용, 짝 누락, 한계)을 `results-v1.md` 또는 새 결과 문서에 먼저 쓴다.
3. 이 표에 한 행을 추가한다. 측정하지 않은 값은 비우지 말고 "출처 확인 필요" 또는 "미측정"으로 적는다.
4. 결정이 생기면 ADR에 기록하고 "결정·상태" 칸에 링크한다.
