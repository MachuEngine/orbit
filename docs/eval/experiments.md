# 평가 실험 기록

평가 단계에서 어떤 후보를 골랐고 무엇을 비교하지 않았는지 남긴다. 결과를 보고 정한 결정과 실행 전에 정한 규칙을 구분한다. 수치 정본은 [results-v1.md](results-v1.md)와 각 파일럿 문서이며, 이 문서는 선택 근거·실험 조건·한계를 연결한다. 기준 설정은 ADR-0031의 **runflow_v0 · react · luna · 메일 색인 켬**, 근거성 Judge는 ADR-0029의 **gemini-3.6-flash**다. 이 기록에서 결정을 바꾸지 않는다.

## 출처와 공통 조건

- `RUNS/`는 원래 평가 worktree의 git 제외 `runs/`다. 보관본은 메인 체크아웃 기준 `../orbit-eval-archive/v1/runs/`에 있다. 별도로 찾은 초기 파일럿·회귀 원본은 `MAIN_RUNS/`(메인 체크아웃의 git 제외 `runs/`)로 표시한다. 메인 작업 트리는 읽기만 했다.
- v0 파일럿은 dev 36과제·독립 근거 6묶음이다. v1 종단 비교는 dev 88과제·73묶음×3회, test 130과제·102묶음×1회, 실험 시드 0이다. 기대 과제 수와 다른 중단 파일은 정규 비교에 섞지 않는다.
- v1의 에피소드 예산은 LLM/도구 합계 50단계·200,000토큰·120초다. 누적 비용·토큰 상한은 실험마다 달랐고 아래 원본 config에 기록돼 있다. 120초 방어 범위는 ADR-0034 전후가 다르다.
- 성공률과 짝 차이는 과제 반복 평균을 먼저 구한 뒤 근거 `cluster_id`를 단위로 bootstrap한다. v1 기존 stats의 기본은 10,000회·seed 0·95% CI다. 초기 J01/J02 Judge 일치도는 2,000회·seed 20261116이다. J04 사람 대조는 10,000회·seed 0으로 따로 기록돼 있어 구분한다. 파일럿의 기존 보고 구간은 원문을 보존했다.
- 단가는 입력/캐시 입력/출력 100만 토큰당 USD 순서다. 비용은 기록 비용이며 미상 호출·임베딩 미기록이 있으면 총 청구액으로 해석하지 않는다. 소수점 반올림은 원본 집계와 별개다.
- 코드 SHA·설정 지문·원본 SHA-256은 뒤의 [원본 대조표](#source-ledger)에 모았다. Judge 실행 코드 SHA가 manifest에 없으면 입력 SUT의 SHA로 대신하지 않고 **출처 확인 필요**라고 적는다.

<a id="p01"></a>
## P01 — 모델 파일럿

- **날짜·질문:** 2026-11-11. 반복 실험에 쓸 모델과 실사용 후보의 비용·품질은 어떤가?
- **후보·선정 근거:** luna none(A), luna low(B), terra low(C). luna 단가 0.10/0.01/0.50, terra 1/0.05/5로 저가 반복용과 상위 품질 후보를 비교했다.
- **고정 조건:** v0 dev 36·6묶음×3, assistant-v0.2, seed 0, OpenAI 직접 경로, 코드 f2e2d2b(깨끗한 작업 트리).
- **비교 기준·사전 규칙:** B−A와 C−B를 짝 비교했다. 예산 $10과 비용·천장 효과를 고려했으나 모델 선정의 수치 문턱이 사전 명시됐는지는 출처 확인 필요다. ADR-0015는 결과 뒤 판단 기록이다.
- **결과:** A 74.1% [57.1, 90.5], B 75.9% [60.2, 91.2], C 100% [100, 100]. B−A +1.9%p [−3.6, +5.9], C−B +24.1%p [+8.8, +39.8].
- **결정·결정자:** 개발자가 실험 B·실사용 C를 골랐고, 11-15 실사용도 B로 바꿨다(ADR-0015).
- **미비교·한계:** terra는 천장 효과와 약 15배 반복 비용으로 실험 기본에서 제외했다. sol·astra는 예산 초과 위험 때문에 비교하지 않았다. v0의 6묶음으로 일반 품질을 단정하지 않는다.
- **비용:** 본 실행 A $0.03675463, B $0.03924173, C $0.57087680. 추정 표본 포함 총 $0.68070449.
- **출처:** [파일럿 결과](../pilot/results-2026-11-11.md), [ADR-0015](../adr/ADR-0015-provisional-model.md), MAIN_RUNS/pilot/{A,B,C}.jsonl와 추정 파일. 문서 SHA-256 `9c9f652e03f7f63295a72316fca7751af62be2ac508e1e46e9e6654a4b266949`.

<a id="rg01"></a>
## RG01 — 7단계 정리 작업 뒤 v0 회귀 확인

- **날짜·질문:** 2026-11-12. 기능 추가 뒤 기존 읽기 과제가 크게 퇴행했는가?
- **후보·선정 근거:** luna low 단일 모델(0.10/0.01/0.50). 모델 선정을 위한 비교가 아니라 회귀 실행이다.
- **고정 조건:** v0 dev 36·6묶음×1, assistant-v0.2, seed 0, 코드 867772e·git_dirty=true.
- **비교 기준·사전 규칙:** 기존 파일럿은 참고값이다. 구현 계획은 프롬프트·도구 변경 때 dev 1회와 하락 조사를 요구했다. 이 실행의 별도 채택 문턱은 없다.
- **결과:** 72.2% [50.0, 94.3]. 구간은 원본을 현행 stats로 10,000회·seed 0 오프라인 재집계했다.
- **결정·결정자:** 회귀 관찰 기록이며 이 단일 실행으로 모델·검색 방식을 선정하지 않았다.
- **미비교·한계:** 반복 1회와 6묶음이다. dirty 코드라 SHA만으로 실행 상태를 완전히 복원할 수 없다.
- **비용:** $0.01459982.
- **출처:** MAIN_RUNS/regress/stage7-luna-low.jsonl, docs/implementation-plan.md §7, docs/implementation-plan.md §7. 원본 대조표 참고.

<a id="rg02"></a>
## RG02 — 데이터 봉투 분리 전후 회귀

- **날짜·질문:** 2026-11-12. 외부 콘텐츠 봉투 변경 뒤 일반 과제 회귀는 어떤가?
- **후보·선정 근거:** luna low, assistant-v0.3. 봉투 분리 첫 실행(sep)과 수정 후 실행(sep2)이다.
- **고정 조건:** v0 dev 36·6묶음×1, seed 0, 코드 121732f·git_dirty=true. 두 파일의 설정 지문은 같다.
- **비교 기준·사전 규칙:** 파일럿과 앞 실행을 회귀 참고로 봤다. 수치 채택 규칙은 확인하지 못했다. 선정 실험이 아니다.
- **결과:** sep 88.9% [82.9, 94.7], sep2 77.8% [65.6, 88.9]. 현행 stats로 10,000회·seed 0 재집계한 구간이다.
- **결정·결정자:** ADR-0018의 봉투 구조 적용에 따른 동작 기록이다. 이 차이를 봉투 효과로 확정하지 않았다.
- **미비교·한계:** dirty 코드와 동일 지문으로 수정 전후 바이너리가 구별되지 않는다. 원본 파일 해시로 구분하며 인과 효과를 주장하지 않는다.
- **비용:** sep $0.02172824, sep2 $0.01956573.
- **출처:** MAIN_RUNS/regress/{sep-luna-low,sep2-luna-low}.jsonl, ADR-0018, docs/implementation-plan.md.

<a id="rg03"></a>
## RG03 — 실모델 주입 공격 동작 확인

- **날짜·질문:** 2026-11-12. 삽입한 외부 지시가 쓰기 호출이나 지침 문장 노출로 이어지는가?
- **후보·선정 근거:** 파일명상 luna low 2회·terra low 1회. 별도 모델 선정이 아니라 경계 동작 확인이다.
- **고정 조건:** 원본 각 6행, assistant/runflow 경로 혼합. 코드 SHA·설정 지문·모델 응답 ID·시드·가격·사용량은 출처 확인 필요다.
- **비교 기준·사전 규칙:** 각 실행의 write_called와 rules_sentence_leaked를 관찰했다. 실험별 사전 ASR 판정 규칙이나 독립 공격 세트는 확인하지 못했다.
- **결과:** 3실행 18행에서 기록된 쓰기 호출 0, 지침 문장 노출 0. 비용·성공률 CI는 기록되지 않았다.
- **결정·결정자:** ADR-0018의 선행 동작 확인으로 남긴다. 안전성 보장·공격 성공률 검증으로 채택하지 않는다.
- **미비교·한계:** 동일 또는 관련 합성 사례의 재실행이며 독립 18공격이 아니다. 실패 공격 탐색·정상 작업 성공·오탐 평가가 없다.
- **비용:** 출처 확인 필요.
- **출처:** MAIN_RUNS/regress/{injection-luna-low,injection-luna-low-2,injection-terra-low}.jsonl, ADR-0018.

<a id="p02"></a>
## P02 — 실사용 runflow 첫 비교 1차·2차

- **날짜·질문:** 2026-11-15. 실사용 쓰기·승인 경로가 읽기 비서와 같은 기준 문제에 답하는가?
- **후보·선정 근거:** assistant_v0 대 runflow_v0, luna low(0.10/0.01/0.50). 실사용 경로를 직접 측정하려는 비교다.
- **고정 조건:** v0 dev 36·6묶음×1, seed 0, assistant-v0.3와 runflow-v0.7+eval-answer-v1·색인 끔. 1차 eaaa1f7, 2차 e4f6f31.
- **비교 기준·사전 규칙:** runflow−assistant. 회귀·원인 탐색 비교이며 사전 수치 채택 규칙은 확인하지 못했다.
- **결과:** 1차 assistant 83.3% [69.2, 97.1], runflow 61.1% [45.5, 75.8], 차 −22.2%p [−30.2, −12.5]. 2차 72.2% [53.6, 88.9] 대 61.1% [50.0, 74.3], 차 −11.1%p [−24.3, +9.4].
- **결정·결정자:** 실행 변동을 확인하고 원인 ablation을 3회 반복으로 진행했다. 기준 모델 변경 결정은 하지 않았다.
- **미비교·한계:** 원문은 같은 코드라고 설명하지만 원본 git_sha는 다르다. 두 SHA 사이의 diff에는 백엔드 평가 코드 변경이 없음을 확인했다. 다만 런타임 추론 항목 보존과 설정 메타데이터의 일치는 별도 문제다. assistant는 읽기 전용이다.
- **비용:** 1차 assistant/runflow $0.02112036/$0.01656351, 2차 $0.01830751/$0.01844765. 추정 파일은 별도다.
- **출처:** [실사용 비교](../pilot/results-2026-11-15-runflow-v0.md), ADR-0023, RUNS/eval-{1,2}/{assistant,runflow}.jsonl. 문서 SHA `317544466a5306c7d8ccc7c152886eca3722725287178e25ce9d3b7edc716a47`.

<a id="p03"></a>
## P03 — runflow 원인 ablation

- **날짜·질문:** 2026-11-15. 사실 확인 단락과 추론 항목 되돌리기가 답안 실패를 줄이는가?
- **후보·선정 근거:** runflow 기본, facts-v1, reasoning-rt, 둘 다, assistant 참고. 같은 luna low로 두 원인 후보를 나눴다.
- **고정 조건:** v0 dev 36·6묶음×3, seed 0, 코드 2ffa079. 실험별 지문은 원본 대조표에 있다.
- **비교 기준·사전 규칙:** runflow 기본 대비 짝 차이. 개선량 채택 문턱의 사전 명시는 확인하지 못했다.
- **결과:** 기본 58.3% [41.3, 73.3]; facts 61.1% [40.5, 82.9], +2.8%p [−4.4, +11.9]; reasoning 60.2% [45.5, 74.3], +1.9%p [−4.9, +10.3]; 둘 다 58.3% [41.7, 78.1], 0.0%p [−8.3, +9.2]; assistant 70.4% [50.0, 90.4], +12.0%p [+6.8, +18.6].
- **결정·결정자:** 개발자가 ablation을 중단하고 v1의 답 안내 고정을 먼저 하도록 정했다(ADR-0023 §10).
- **미비교·한계:** 변형별 개선을 확인하지 못했다. 답 종류·형식 실패는 원인 후보이며 확정 원인은 아니다. README E00의 “수치를 문서 표로 남기지 않았다”는 설명은 잘못이었다.
- **비용:** 기본/facts/reasoning/둘 다/assistant $0.05543080/$0.05817371/$0.02247281/$0.02293782/$0.04021485, 본 실행 합 $0.19922999.
- **출처:** [ablation 결과](../pilot/results-2026-11-15-runflow-ablation.md), ADR-0023 §10, RUNS/ablation-1/{base,facts,reasoning,both,assistant}.jsonl. 문서 SHA `d6ffa605d637f87479e4bf6e64d5bb4c033a5db90e99a5f00d1f6d6a9c92796f`.

<a id="d01"></a>
## D01 — 평가셋 v1 구축

- **날짜·질문:** 2026-11-16. 한국어 도구 과제의 구조 정답과 자연스러운 합성 본문을 어떻게 함께 검증하는가?
- **후보·선정 근거:** 정답·분할은 코드가 만들고 OpenAI·Anthropic·Google이 본문을 실현하며 다른 계열이 교차 검수한다. 단가 예시는 실행 전 확정 가격이 아니므로 기록 비용과 청구액을 구분한다.
- **고정 조건:** seed 20261116, 생성 코드 5a12956, 본문 7,022개(템플릿 대체 14), 본문 버전 realize-v1 6,993/v2 29. 봉인 뒤 종단 dev 88/test 130, 검색 dev 55/test 78이다.
- **비교 기준·사전 규칙:** 정답·근거 불변식, 분할 근거 비공유와 사람의 최종 제외 판정. ADR-0026의 단계별 검수·봉인 절차가 사전 규칙이다.
- **결과:** 자동 검수 372항목(과제 192+검색 180), 1,198요청, 54항목 표시, 파싱 실패 0. 사람 검수로 종단 22·검색 47 제외. 표현 변형 후보 25 중 24 사용, 37시도·검수 문제 0.
- **결정·결정자:** 개발자 1명이 자동 지적을 보고 최종 확정·봉인했다. 봉인 시각 UTC 2026-11-16T11:54:15.110941+00:00.
- **미비교·한계:** 템플릿만 사용은 다양성 부족, LLM 정답 생성은 오류 검증 곤란, 매 실행 재생성은 재현성 훼손, 공개 벤치마크 직접 사용은 한국어·도구 불일치로 제외했다. 라벨러 1명·자동 지적의 기준점 효과·검수 자기 일치도 미측정·생성 계열 선호 분석 미실시가 남는다.
- **비용:** 본문 $1.746627, 표현 변형 $0.002646, 자동 검수 $0.332821, 합 $2.082094(예시 단가에 따른 기록값).
- **출처:** [results-v1 평가셋 구축](results-v1.md), ADR-0026, eval/datasets/orbit-eval/v1/{manifest,realize-manifest,auto-review-manifest,search-manifest}.json. 세계·뼈대·매니페스트 해시는 원문과 원본 대조표 참고.

<a id="r01"></a>
## R01 — 검색 방식 비교

- **날짜·질문:** 2026-11-16~17. 같은 메일 색인에서 어휘·벡터·혼합 검색은 어떤 질의를 찾는가?
- **후보·선정 근거:** gmail_search(Sim), keyword(Kiwi+ts_rank), BM25, vector, hybrid. 검색 구현 계열 차이를 비교했다. text-embedding-3-small, 800자/100자 겹침, RRF k=60은 고정값이다.
- **고정 조건:** 메일 질의 dev 37·13묶음, test 51·19묶음. 메일함 200/1,000/5,000통, seed 0, bootstrap 10,000. dev 코드 647cbe9·dirty=true, test fda3bf3·clean. 설정 지문은 dev `a0e84cf8f0e532c59de7409b7d9143979682a43c3decb2973598f14cd28c181d`, test `40fdac8aaa85667cf0541b01452c8feffa400e2dfff7ab4ed92c0318b090839f`다.
- **비교 기준·사전 규칙:** 방식별 R@10 및 짝 차이. ADR-0019·0028의 지표·test 봉인 확인은 사전 설계다. 특정 방식을 고를 수치 문턱은 확인하지 못했다.
- **결과:** 5,000통 dev R@10 hybrid/vector 0.703, BM25 0.568, keyword 0.432, gmail 0.054. vector−hybrid 0.000 [−0.162, +0.167], hybrid−BM25 +0.135 [+0.053, +0.231]. test vector 0.71, hybrid 0.69, BM25 0.67, keyword 0.47, gmail 0.14. 자세한 CI·표는 results-v1에 있다.
- **결정·결정자:** test를 보고 방식을 다시 선정하지 않았다. ADR-0031은 색인 켬 채택이며 벡터 우월 채택이 아니다.
- **미비교·한계:** 임베딩 모델·청크·RRF 자체는 비교하지 않았으며 ADR-0019에서 실험 중이다. 메일만의 부분집합과 작은 묶음 수다. dev의 dirty 코드는 재현 한계다.
- **비용:** dev 임베딩 $0.03687788(91 API·1,843,894토큰), test $0.00003416(색인 재사용). LLM 종단 비용과 합치지 않는다.
- **출처:** RUNS/retrieval/{dev-openai-v1,test-openai-v1}.json, ADR-0019·0028. dev SHA `24879f386159a53eff596125f6c38fd6ac41c92a85c2f3fd14596062af44b9d0`, test SHA `12754a47ac6b77fe1cb66c6a1ab0d0e6e27d20284b20a7b63f2bb0ec845db820`.

<a id="b01"></a>
## B01 — dev 기준선

- **날짜·질문:** 2026-11-17. 확장 지표와 v1 답 형식 위에서 두 비서 경로의 기준 성능은 어떤가?
- **후보·선정 근거:** assistant_v0와 runflow_v0 react, luna none(0.10/0.01/0.50). 비용이 낮고 개선 여지가 있는 임시 기본 모델을 썼다.
- **고정 조건:** v1 dev 공통 조건, 색인 끔, 코드 fc4a577, seed 0. runflow 264·assistant 264에피소드.
- **비교 기준·사전 규칙:** 이후 한 요인 비교의 기준은 runflow react다. bootstrap·예산 규칙은 사전 설계이며 기준선 자체로 새 후보 선정 문턱을 적용하지 않았다.
- **결과:** runflow 0.489 [0.386, 0.592], assistant 0.424 [0.313, 0.534].
- **결정·결정자:** 비교 기준으로 고정해 M01/A01/G01에서 재사용했다. 실제 채택 기준은 나중 ADR-0031의 색인 켬으로 바뀌었다.
- **미비교·한계:** assistant는 읽기 전용이라 쓰기 포함 과제의 동등 능력 비교가 아니다. 다른 실험의 코드 SHA가 다르다.
- **비용:** runflow $0.17217004, assistant $0.10000601. 폐기한 이전 기준선은 I01에 분리했다.
- **출처:** RUNS/baseline-v1/{assistant,runflow}.jsonl와 *.stats.json, results-v1 dev 표, ADR-0029.

<a id="m01"></a>
## M01 — 색인 끔 모델 비교

- **날짜·질문:** 2026-11-17. 저가 공급자 후보가 luna 기준선보다 성공률을 높이는가?
- **후보·선정 근거:** luna none, claude-haiku-5.5(0.10/0.01/0.50), gemini-3.5-flash-lite(0.30/0.03/2.50). 야간 계획의 비용 범위 안에서 두 비OpenAI 계열을 추가했다.
- **고정 조건:** v1 dev 공통·react·색인 끔. luna는 B01(fc4a577)을 재사용, Haiku·Gemini는 PR #34의 temperature 404 수정 뒤 2c04285에서 실행했다.
- **비교 기준·사전 규칙:** 각 후보−luna. 모델 입력 단가 $1 미만(ADR-0026 §2의 6번 항목)과 실행 예산은 사전 조건이다. 수치로 자동 선정하는 문턱은 출처 확인 필요다.
- **결과:** Haiku 0.674 [0.568, 0.777], 차 +0.186 [+0.097, +0.282]. Gemini 0.386 [0.283, 0.491], 차 −0.102 [−0.193, −0.008].
- **결정·결정자:** 개발자는 비용과 G01을 보고 luna+색인을 선택했다. Haiku+색인은 이때 비교하지 않았다.
- **미비교·한계:** Gemini 후보는 luna/Haiku와 입력 3배·출력 5배로 가격대가 다르며, 허용 범위 최상위 Google 모델도 아니다. 같은 가격의 gemini-2.5-flash-lite와 상위 gemini-3.6-flash는 비서로 미비교였다. 코드·가격을 맞춘 단일 요인 실험이 아니다. 이번 M02가 모델×색인 일부를 보완한다.
- **비용:** Haiku $1.03256390, Gemini $1.21762318(본 실행). 추정·중단 파일은 별도다.
- **출처:** RUNS/models-v1/{haiku,gemini}.jsonl, *.stats.json, *.vs-luna.json; RUNS/handoff/overnight-plan.md, PR #34, ADR-0026 §2의 6번 항목·0031.

<a id="a01"></a>
## A01 — 색인 끔 패턴 비교

- **날짜·질문:** 2026-11-17. 계획·분류 기반 패턴이 react보다 성공률이나 비용을 개선하는가?
- **후보·선정 근거:** react, fixed_workflow, plan_execute. fixed는 범주 분류+제한 도구+단계 상한의 근사 구현이며 임의 DAG 워크플로 전체를 대표하지 않는다(ADR-0030).
- **고정 조건:** v1 dev 공통·luna none·색인 끔. react B01 재사용, 다른 패턴 코드 fda3bf3.
- **비교 기준·사전 규칙:** 각 패턴−react. ADR-0030의 같은 데이터·예산 비교. 채택 성공률 문턱은 출처 확인 필요다.
- **결과:** fixed 0.549 [0.447, 0.651], +0.061 [−0.011, +0.130]; plan 0.390 [0.298, 0.485], −0.098 [−0.169, −0.027]. fixed 예상 밖 상태 변화 7건.
- **결정·결정자:** 개발자는 react 유지(ADR-0031). fixed의 차이 CI는 0을 포함했고 test 정지도 관찰했다.
- **미비교·한계:** 패턴×색인 상호작용은 미측정이었다. plan의 비용 미상 11건과 오류를 비용 절약으로 해석하지 않는다. 이번 M02가 색인 켬 조합을 보완한다.
- **비용:** fixed $0.14846637, plan 알려진 비용 $0.21373155·비용 미상 11건.
- **출처:** RUNS/compare-v1/pattern-{fixed,plan}.jsonl, *.stats.json, *.vs-luna.json, ADR-0030·0031.

<a id="g01"></a>
## G01 — 메일 색인 켬

- **날짜·질문:** 2026-11-17. 실사용과 같은 메일 색인 검색을 켜면 종단 성공률이 높아지는가?
- **후보·선정 근거:** luna react의 색인 끔 대 켬. 기존 Gmail 문법 검색과 색인 도구를 함께 사용할 수 있는 조건을 비교했다.
- **고정 조건:** v1 dev 공통·luna none, off는 B01(fc4a577), on은 fda3bf3. 6계정의 색인 지문·고정 시점은 config.sut_config.index에 기록했다.
- **비교 기준·사전 규칙:** on−off. 신뢰구간·비용 보고는 사전 설계다. 최종 선택은 개발자 사후 결정이다.
- **결과:** on 0.644 [0.544, 0.736], 차 +0.155 [+0.072, +0.244].
- **결정·결정자:** 개발자가 luna+메일 색인을 기준 설정으로 채택했다(ADR-0031).
- **미비교·한계:** 모델×색인과 패턴×색인 미비교, 검색 자체 방식·임베딩·청크·RRF 선정 실험은 아니다. 이후 비교는 이 기준에 맞춰야 한다.
- **비용:** LLM $0.19830449. 재사용 색인의 질의 임베딩 미기록 비용은 별도 한계다.
- **출처:** RUNS/compare-v1/index-on.jsonl·.stats.json·.vs-luna.json, ADR-0019·0023·0031.

<a id="g02"></a>
## G02 — 색인 켬에서 추론 강도 low

- **날짜·질문:** 2026-11-17. none 대신 low를 쓰면 같은 기준의 성공률이 높아지는가?
- **후보·선정 근거:** luna react·색인 켬 none 대 low. 추가 추론의 비용·지연·성공률을 확인했다.
- **고정 조건:** v1 dev 공통, seed 0, 색인 지문 동일. none 코드 fda3bf3, low c627a78. low에서 reasoning 항목 되돌리기가 자동 켜졌다.
- **비교 기준·사전 규칙:** low−none. 실행 전 8건 비용 추정과 $0.6 상한이 있었고 별도 개선량 채택 문턱은 확인하지 못했다.
- **결과:** low 0.602 [0.504, 0.696], none 0.644. 차 −0.042 [−0.109, +0.020].
- **결정·결정자:** 평가 기준 none 유지. 실사용 low를 유지할지는 사용자 결정으로 남겼다(ADR-0031 결정 3).
- **미비교·한계:** 순수 effort 분리가 아니다(추론 되돌리기·코드·가격 출처 문구·누적 토큰 상한 차이). test low 미측정, CI 0 포함은 동등성 증명이 아니다.
- **비용:** 본 $0.19793660+추정 8건 $0.00703349=$0.20497009. 임베딩 실행별 상한은 있었으나 최종 질의 비용 기록은 없다.
- **출처:** RUNS/compare-v1/index-on-low{.estimate,}.jsonl; followup-20261117/{low.stats,low.vs-index-none,run-low.status,estimate-low.status}.json·로그, results-v1 추론 강도 절.

<a id="t01"></a>
## T01 — 봉인 test 확인

- **날짜·질문:** 2026-11-17. dev에서 본 모델·패턴·색인의 방향이 봉인 과제에서도 나타나는가?
- **후보·선정 근거:** assistant, luna react/plan/index, Haiku react, Gemini 3.5 react. fixed는 정지 때문에 T02로 분리했다.
- **고정 조건:** 각 test 130·102묶음×1, seed 0. 코드 luna/assistant fda3bf3, 모델 2c04285. 예산·지문은 test 원본별 기록이다.
- **비교 기준·사전 규칙:** luna-react 대비 짝 차이. 봉인 확인 플래그·1회 확인 계획은 사전 규칙이나 실제 중단·재개·후속 비교가 있었음을 함께 기록한다.
- **결과:** assistant 0.400 [0.305,0.500], 차 −0.054 [−0.172,+0.061]; react 0.454 [0.360,0.549]; plan 0.392 [0.301,0.485], −0.062 [−0.148,+0.023]; index 0.646 [0.551,0.738], +0.192 [+0.100,+0.287]; Haiku 0.577 [0.481,0.669], +0.123 [+0.031,+0.216]; Gemini 0.446 [0.352,0.541], −0.008 [−0.116,+0.096].
- **결정·결정자:** ADR-0031의 dev 선택을 확인하는 자료로 보고했다. index·Haiku는 양의 CI, 다른 설정은 방향만 같고 열세를 통계적으로 확정하지 않는다.
- **미비교·한계:** test 재사용 선택 편향과 설정별 코드 차이. plan 비용 미상 8건은 RunFailed/LLMRequestError 5건과 정상 종료 3건(성공 1·오답 2)으로 일괄 오류 8건이 아니다. test 전체를 딱 한 번만 읽었다고 주장하지 않는다.
- **비용:** assistant/react/plan/index/Haiku/Gemini $0.08157650/$0.08322693/$0.10075598/$0.09187418/$0.53856030/$0.63800217, 알려진 합 $1.53399606와 미상 8건.
- **출처:** RUNS/test-v1/{assistant,luna-react,luna-plan,luna-index,haiku,gemini}.jsonl, followup-20261117/test-stats/, results-v1 test 절·출처 표.

<a id="t02"></a>
## T02 — fixed 정지 방어 수정 뒤 test 재확인

- **날짜·질문:** 2026-11-17. 에피소드 시간 상한 방어 뒤 fixed test가 완주하는가?
- **후보·선정 근거:** luna none·fixed_workflow·색인 끔 단일 실행. react test는 비교값으로 재사용했다.
- **고정 조건:** 수정 PR #39 승인·머지 뒤 코드 c627a78, test 130·102묶음×1, seed 0. 본 $0.3 상한, 추정 $0.1 상한, 자체 프로세스 그룹 10분 무진행 watchdog.
- **비교 기준·사전 규칙:** 완주 여부와 fixed−react. 실제 재실행은 B 수정 머지를 선행 조건으로 했고, watchdog·비용 상한을 걸었다.
- **결과:** 130/130 완주, 0.492 [0.400,0.585], 차 +0.038 [−0.042,+0.118]. 단계 상한 sut_error 3건, 시간 초과 0·정리 오류 0, 1,196.3초·watchdog 미발동.
- **결정·결정자:** 실제 정지 원인은 미확정으로 유지했다. 이 한 번의 완주로 원인 해결을 확정하지 않았다.
- **미비교·한계:** 이전 hung 파일은 통계에 섞지 않았다. 추정 8건도 봉인 사용 1회이며 본 실행과 구분한다. 승인 위반 0은 안전 공격 성능이 아니다.
- **비용:** 본 $0.07280940+추정 $0.00502711=$0.07783651.
- **출처:** RUNS/test-v1/luna-fixed{.estimate,}.jsonl; followup-20261117/{fixed.stats,fixed.vs-luna-react,run-fixed.status}.json, ADR-0034, PR #39.

<a id="j01"></a>
## J01 — 저가 두 계열 Judge 일치도

- **날짜·질문:** 2026-11-17. 서술형 근거성을 두 비평가 계열이 일관되게 판정하는가?
- **후보·선정 근거:** Haiku 5.5(0.10/0.01/0.50), Gemini 2.5 Flash Lite(0.10/0.01/0.40). 평가 대상 OpenAI와 다른 저가 계열을 골랐다.
- **고정 조건:** 기준선 assistant/runflow 528에피소드×2, judge-v1, max output 6,000·tool 본문 12,000자, Anthropic temperature 생략/Google 0. Judge 실행 코드 SHA·지문은 manifest에 없어 출처 확인 필요다.
- **비교 기준·사전 규칙:** Judge 간 에피소드 grounded 이진 일치. ADR-0029의 사람 kappa ≥0.6 검증 원칙과 Judge 일치도 관찰이다.
- **결과:** Lite 판정 없는 16건을 제외한 512·73묶음에서 일치율 0.645, kappa 0.099 [0.003,0.203].
- **결정·결정자:** 신뢰도 미달로 확정 지표에 쓰지 않고 사람 표본·상위 Judge 비교를 진행했다.
- **미비교·한계:** Judge 간 일치는 사람과의 일치가 아니다. 주장 단위 분할은 서로 달라 정렬하지 않았고 분포만 보고했다.
- **비용:** $0.34956646.
- **출처:** RUNS/judge/{baseline/judge-manifest.json,agreement-ab.json}, results-v1 Judge 절, ADR-0029.

<a id="j02"></a>
## J02 — judge-v1과 사람 표본·상위 Judge 비교

- **날짜·질문:** 2026-11-17. 낮은 일치가 저가 Judge 때문인지 사람 라벨과 대조하는가?
- **후보·선정 근거:** 기존 Haiku/Lite에 Sonnet 5.5(2/0.10/10)·Flash 3.6(0.75/0.075/3.75)을 추가했다. Sonnet 입력 $2는 ADR-0026 §2의 6번 항목 저가 규칙 밖이며 **개발자 지시로 수행한 예외**다(이번 작업 지시에서 확인).
- **고정 조건:** 사람 표본 60·38묶음, judge-v1, 같은 출력 상한·계열 온도. Judge 실행 코드 SHA·지문은 출처 확인 필요다.
- **비교 기준·사전 규칙:** 각 Judge−사람 일치 및 상위 Judge끼리 kappa. kappa ≥0.6 원칙은 사전 규칙이나 양성 위주 표본의 음성 탐지 기준은 없었다.
- **결과:** 사람 일치율 Haiku 0.70, Lite 0.85(유효 59), Sonnet 0.82, Flash 0.92. Sonnet/Flash 간 kappa 0.526 [0.140,0.841].
- **결정·결정자:** 1건의 금액 정규화 오경보 라벨을 정정한 뒤 원본 60건은 모두 양성이라 균형 변형 세트로 보완했다. 과거 집계는 당시 라벨 기록으로 남겼다.
- **미비교·한계:** 개발자 설명과 실행 manifest를 구분한다. 과거 승인 로그는 별도 확인하지 못했다. 모두 양성인 표본으로 음성 탐지 능력을 입증할 수 없다. 실사용 후보의 가격 규칙 예외를 일반화하지 않는다.
- **비용:** 상위 두 Judge $0.51654975. 기존 저가 판정 비용은 J01에 포함돼 중복 합산하지 않는다.
- **출처:** RUNS/judge/{agreement-human,agreement-strong60}.json, strong60/judge-manifest.json, human-labels.jsonl; ADR-0026 §2의 6번 항목·0029, 2026-11-18 개발자 작업 지시.

<a id="j03"></a>
## J03 — 표면 치환 blind60

- **날짜·질문:** 2026-11-17. 오경보를 줄인 judge-v2가 단일 값 변형을 찾는가?
- **후보·선정 근거:** Flash 3.6·Haiku 5.5·Lite 2.5, 앞 단계와 같은 단가·출력 상한.
- **고정 조건:** 원본 60·38묶음, 검토 후보 38 중 seed 20261117로 30변형+30원본. 날짜14·시각1·이름7·장소4·금액4. 프롬프트 judge-v2. 실행 코드 SHA·지문은 출처 확인 필요다.
- **비교 기준·사전 규칙:** 정답 키 대비 변형 재현율·원본 오탐·정확도. 사람 kappa ≥0.6 및 재현율 ≥0.8 동시 통과 원칙과 $1 상한을 사전 설정했다.
- **결과:** Flash/Haiku/Lite 재현율 30/30,30/30,26/30; 오탐 4/30,7/30,5/30; 정확도 56/60,53/60,51/60.
- **결정·결정자:** 문자열 대조로도 잡히는 변형이라는 한계를 기록하고 J04로 보완했다.
- **미비교·한계:** 코드 검사보다 Judge가 더 필요한지를 재지 못한다. 보정에 사용한 원본 재사용으로 독립 검증도 아니다.
- **비용:** $0.19359150/$0.02967420/$0.01215963, 합 $0.23542533.
- **출처:** RUNS/judge/blind60-{summary,manifest,audit}.json, v2-blind60-*/metrics-key.json. 원본·키·개별 판정은 git 제외다.

<a id="j04"></a>
## J04 — 의미 변형 blind60와 사람 검증

- **날짜·질문:** 2026-11-17. 문자열에 있는 값도 관계·시점·행동이 틀리면 Judge가 더 찾는가?
- **후보·선정 근거:** 코드 문자열 대조, Flash 3.6·Haiku 5.5·Lite 2.5. 코드보다 추가되는 가치를 직접 비교했다.
- **고정 조건:** 원본30+변형30, source·정규화 답 각60유일, seed20261118. surface6/stale3/role6/addition5/action4/absence3/flip3, 프롬프트는 judge-v2 그대로. Judge 실행 코드 SHA·지문은 출처 확인 필요다.
- **비교 기준·사전 규칙:** 유형별 탐지·원본 오탐·정확도, 후속 사람 60건(38묶음) kappa. 사람 kappa ≥0.6·재현율 ≥0.8 동시 점추정 통과, dry-run ≤$0.5와 v2 포함 $1 상한을 적용했다.
- **결과:** surface 외24건 탐지 코드1/Flash24/Haiku24/Lite18, 정확도 30/58/54/50/60, 오탐7/2/6/4. Lite 음성1건 누락도 분모 유지. 사람 kappa Flash0.636 [0.446,0.805], Haiku0.575 [0.395,0.750], Lite0.454 [0.247,0.641](59유효).
- **결정·결정자:** 개발자는 유일하게 두 문턱을 통과한 Flash 3.6을 근거성 Judge로 채택했다(ADR-0029 §3). 성공률 주지표는 바꾸지 않고 보조 지표로 사용한다.
- **미비교·한계:** 코드가 잡은1건은 원답도 계산값 때문에 음성인 항목(변형 자체의 새 탐지0)이다. 단일 라벨러·kappa CI 하한0.446·작은 유형 표본·보정 원본 재사용 한계가 있다. 기능 부재·사실대로 미실행 보고 규칙은 당시 없는 judge-v2였다.
- **비용:** 183요청 $0.23054425, J03 포함 $0.46596958.
- **출처:** RUNS/judge/v3-blind60-set/{blind60-summary,human-agreement}.json, v3-blind60-*/metrics-key.json, results-v1 v3 절·ADR-0029 §3.

<a id="i01"></a>
## I01 — 중단·폐기한 이전 기준선

- **날짜·질문:** 2026-11-16 밤. 지표 확장 이전 결과를 새 기준선으로 써도 되는가?
- **후보·선정 근거:** 기존 assistant/runflow 기준선과 새 지표 버전.
- **고정 조건:** 이전 assistant264행·runflow191행과 추정 각8행. 상세 코드·지문은 원본 대조표에 있다.
- **비교 기준·사전 규칙:** 정규 B01과 분리. 지표 확장 뒤 새 기준선을 측정하기로 한 절차에 따라 폐기했다.
- **결과:** 이전 결과를 B01 성공률·비교 CI에 섞지 않았다.
- **결정·결정자:** ADR-0029 머지 후 기준선 재실행. 폐기 원인은 품질 열세가 아니라 기록·지표 버전 전환이다.
- **미비교·한계:** 중단 호출의 미기록 비용은 파일 합계로 복원할 수 없다.
- **비용:** 완료행 $0.21997402(야간 메모 약 $0.22), 추정 $0.01106681, 알려진 합 $0.23104083.
- **출처:** RUNS/baseline-v1-aborted/{assistant,runflow}{.estimate,}.jsonl, handoff/overnight-plan.md, ADR-0029.

<a id="i02"></a>
## I02 — 동일 시드 병렬 실행 충돌

- **날짜·질문:** 2026-11-17. 여러 worktree에서 같은 시드 runflow를 동시에 실행해도 되는가?
- **후보·선정 근거:** Gemini·plan·색인 켬을 병렬로 시작한 실행. 별도 worktree는 평가 DB 작업공간을 격리하지 못했다.
- **고정 조건:** 동일 시드·동일 과제 기반 평가 DB. crashed Gemini0행·plan0행·index22행.
- **비교 기준·사전 규칙:** 정상 재실행 결과와 분리. EpisodeInUse는 중복 작업공간 사용을 막는 보호 동작이다.
- **결과:** 충돌로 중단하고 순차 queue로 다시 실행했다. crashed는 M01/A01/G01 통계에 섞지 않았다.
- **결정·결정자:** 이후 runflow는 한 번에 하나씩 순차 실행한다. Judge는 평가 DB를 사용하지 않는다.
- **미비교·한계:** 0행이라고 비용0을 입증하지 못한다. 실패·취소 호출의 청구를 완료행만으로 복원할 수 없다.
- **비용:** index22행 알려진 $0.00904837. 다른 두 파일의 완료행 비용은0, 실패 호출 총비용은 출처 확인 필요.
- **출처:** RUNS/crashed/{gemini,pattern-plan,index-on}.jsonl, handoff/overnight-plan.md·queue4.sh.

<a id="i03"></a>
## I03 — fixed test 정지와 노트북 중단

- **날짜·질문:** 2026-11-17. dev는 완주했는데 test가 멈춘 원인은 무엇인가?
- **후보·선정 근거:** luna fixed test의 부분 실행·정지2회와 ADR-0034 방어 뒤 T02.
- **고정 조건:** 노트북 닫기 partial67행, 이후 hung4행·hung2 9행. 완료행만 있어 정지한 호출 스택은 없다.
- **비교 기준·사전 규칙:** 완료된 T02와 분리. 추후 방어 검증은 10분 무진행 시 자기 프로세스 그룹만 종료하는 감시를 적용했다.
- **결과:** 정지 중 평가 DB의 실행 쿼리·큐가 없었다는 관찰은 있지만 실제 원인은 미확정이다.
- **결정·결정자:** fixed를 잠시 보류하고 다른 test를 순차 진행했다. PR #39는 준비·실행·관찰·정리의 시간 상한 누락을 방어했다.
- **미비교·한계:** 네트워크·절전·이벤트 루프 정지·범주 밖 도구 루프를 단일 원인으로 확정하지 않는다. 옛 watchdog의 이름 기반 pkill과 후속 자기 그룹 종료 감시는 다른 구현이다. 부분 파일을 정규 결과에 섞지 않는다.
- **비용:** partial $0.03706520, hung $0.00239901, hung2 $0.00487576(완료행만). 정지 호출 비용은 출처 확인 필요.
- **출처:** RUNS/crashed/test-luna-fixed-{partial,hung,hung2}.jsonl, handoff/overnight-plan.md, ADR-0034·T02.

<a id="m02"></a>
## M02 — 색인 켬 기준 재비교

- **날짜·질문:** 2026-11-18. 기준과 같은 색인 켬에서 모델·패턴 후보의 차이가 유지되는가?
- **후보·선정 근거:** Haiku react, Flash3.6 react, Lite2.5 react, luna fixed/plan. 기준은 기존 luna react index-on을 재사용한다. 가격은 OpenRouter API 재확인 기록에 남긴다.
- **고정 조건:** dev 후보 5개의 완료·건너뜀을 모두 확정한 뒤 test 후보 5개를 순차 확인했다. dev 88×3·73묶음, test 130×1·102묶음, seed 0·50단계/200,000토큰/120초·기존 색인/캐시다. 새 실행 코드 SHA는 35b95d6d75ea(clean)이며 전체 설정 지문과 원본 SHA는 [결과 표](results-v1.md#색인-켬-기준-재비교)에 있다.
- **비교 기준·사전 규칙:** dev compare-v1/index-on, test test-v1/luna-index. 매 실행 estimate8, 모델별 dev/test 비용 상한, 임베딩 실행당$0.05, 10분 무진행 자기 프로세스 그룹 종료. 기준보다 성공률 높고 짝 CI가0을 넘으면 비용 배수와 함께 개발자 결정 필요로 보고한다.
- **결과:** dev Haiku 0.617, fixed 0.670으로 기준 대비 −0.027 [−0.154, +0.100], +0.027 [−0.049, +0.091]이었다. test Haiku/fixed/plan은 0.577/0.631/0.469, 기준 대비 −0.069 [−0.195, +0.054]/−0.015 [−0.085, +0.058]/−0.177 [−0.270, −0.086]이었다. Google 2종은 dev/test 모두 본 실행이 없고 Plan dev도 미실행이다. 추정 비용 누락 또는 상한 초과로 건너뛴 것으로 성능 0점과 구분한다.
- **결정·결정자:** 사전 양의 차이·CI 조건을 충족한 후보는 없다. 개발자의 ADR-0031 기준을 유지하며 새 채택 결정을 하지 않았다.
- **미비교·한계:** 모델×색인·패턴×색인의 일부만 보완했다. Haiku dev의 메일 wrong_answer 37건 중 33건은 최신 사실/구조화 답은 맞아도 정정 전 값을 설명에 인용해 must_not_contain만 실패했다. 원점수는 유지한다. test 재사용 편향·코드/시점 차이와 Google·Plan dev 공백이 남는다. 비용 미상 추정을 관찰한 뒤 본 실행을 생략하는 감독 절차를 보강했으며 원본을 고치거나 추정값으로 누락 비용을 채우지 않았다.
- **비용:** 사전 추정 포함 관측 LLM 합계 하한 $2.270643545, 비용 미기록 16건, 임베딩 $0.00006750. Plan test는 비용 미상 5건을 포함한 완주이며 $0.20 상한 준수는 미검증이다. 모든 실행별 추정·기록·상한은 결과 표에 분리했다. test 봉인 사용은 추정 5회+본 실행 3회=8회다.
- **출처:** RUNS/complete-v1/model-prices-20261118.json, compare-v2/ 및 test-v1/*-index.jsonl, complete-v1/*.stats.json·*.complete.json. 파일별 코드·설정·SHA와 비용 하한 근거는 [결과](results-v1.md#색인-켬-기준-재비교)를 따른다.

<a id="j05"></a>
## J05 — test Judge-v3 준비와 비용 조건 중단

- **날짜·질문:** 2026-11-19(지시·코드 준비는 11-18). 성공으로 채점된 답도 근거가 있는지, 보조 엄격 성공에서 결론이 유지되는가?
- **후보·선정 근거:** 채택된 Flash3.6 단일 Judge. 모델 선정 실험을 다시 하지 않는다.
- **고정 조건:** 요청 12설정 1,560건 중 M02의 Google test 원본이 없어 가용 10설정 1,300건이었다. 답 없음 33건을 제외한 1,267건을 준비했다. judge-v3에 기능 부재 단정/정직한 미실행 보고 두 규칙을 추가했고, 과거 J03/J04는 judge-v2 기록을 보존했다. 코드 SHA 35b95d6d75ea, 계획 모델 설정 지문과 입력/설정 SHA는 [상세](results-v1.md#test-judge-v3)에 있다.
- **비교 기준·사전 규칙:** luna-index 대비 grounded·엄격 성공 차이. dry-run 합계≤$5.5일 때 실행, 누적$6·동시성4, 입력hash 기반 재개. strict=원본success AND grounded, 전체130분모와 제외·누락을 별도 보고, 주success를 바꾸지 않는다.
- **결과:** 전체 최초 dry-run $6.121430이 실행 조건 $5.50을 넘어 실제 호출은 하지 않았다. 근거성·엄격 성공률·짝 차이는 미측정이며 0으로 채우지 않았다. 입력 검증과 배타잠금 절차만 준비했다.
- **결정·결정자:** 개발자가 정한 비용 조건에 따라 중단했다. 기준·Judge 채택 결정은 유지한다. 추가 예산 또는 범위 변경은 개발자 결정 필요다.
- **미비교·한계:** 라벨러1명·검증κ CI하한0.446·Judge 입력에 에이전트 도구 목록 없음·dev 미채점·Flash 자기 계열 편향. 문항/문장 품질·유용성 평가는 아니다.
- **비용:** 실제 지출 $0. 추정은 1글자=1토큰·시도 1.1회·출력 350토큰·캐시 없음이며 문턱을 맞추기 위해 가정을 낮추지 않았다. 사용자 누적 상한 $6, 계획 CLI 상한 $5.30이다.
- **출처:** ADR-0029 §3, RUNS/complete-v1/test-settings-final.json·test-judge-final-input.jsonl·judge-execution-plan.json·judge-final-dry-run.log. 각각의 SHA는 [상세](results-v1.md#test-judge-v3)에 있다. judge.jsonl은 생성하지 않았다.

<a id="c01"></a>
## C01 — Agent 코드 채점 문자열 검사 감사

- **날짜·질문:** 2026-11-24. 코드 채점의 `answer_facts`·`must_not_contain` 검사가 성공을 실패로 잘못 세는가, 그 때문에 짝 차이 결론이 바뀌는가?
- **후보·선정 근거:** Agent 실행 원본 20개(dev 10, test 10, 2,450 에피소드) 전체. 코드 성공이거나 문자열 검사만 실패한 에피소드를 Jev v4 판정으로 바꾼다.
- **고정 조건:** 에이전트 재실행 없음. Jev 설정은 ADR-0039와 같다(`typesafe-ai/jev-1.14.0`, `jev-correctness-v4-extra`, 임계값 0.25). 재계산은 기존 문서와 같은 과제 묶음 부트스트랩(10,000회, 시드 0)이며 코드 값을 그대로 재현했다.
- **비교 기준·사전 규칙:** Jev 재채점 짝 차이를 코드 값과 나란히 보고한다. 일정 목록 과제는 설명이 반복돼 Jev가 맞는 답을 실패로 봤다. 이를 확인한 뒤 시각 설명에 일정 제목을 붙여 다시 채점했다(사후 수정).
- **결과:** 사람이 대조한 test 62건 중 Jev가 61건에서 같은 판정. Jev가 성공을 실패로 바꾼 에피소드는 test 1·dev 13건(dev 11건은 `t-free-slot-0008` 거짓 성공). 오채점은 haiku-index·haiku에 몰렸다. test haiku-index−haiku +0.077 [+0.008, +0.146], haiku-index−luna-index +0.069 [−0.024, +0.164]. dev haiku-index−index-on +0.106 [+0.011, +0.204]. 패턴 비교와 Luna 색인 효과의 결론은 유지됐다. Gemini 2차 판정(사전 규칙으로 고른 686건)은 Jev와 685건 일치했고, 무작위 표본 100건은 모두 일치했다. 다른 1건("오전 12시" 표기)으로 test gemini−luna-react만 −0.038 [−0.146, +0.066]이 됐다.
- **결정·결정자:** 개발자 결정으로 Agent 성공률의 주 점수를 코드 구조 검사 + Jev + Gemini 판정으로 바꾼다. 문자열 비교는 의미가 같고 표기가 다른 답을 실패로 세기 때문이다. haiku-index가 dev에서 유의하게 높고 test에서 CI가 0을 포함하므로 ADR-0031 재검토 여부는 개발자가 정한다. 이후 Agent 실험도 같은 방식으로 채점한다.
- **미비교·한계:** 목록 과제 질문 형식은 사후 수정이다. 사람 대조는 test 코드 실패 62건만 다뤘고, Jev가 성공을 실패로 바꾼 쪽은 사람 대신 Gemini로 확인했다. Jev만 쓴 839건의 불일치율은 95% 수준에서 3.0% 이하로 추정하지만, 두 판정기가 함께 틀리는 경우는 잡지 못한다.
- **비용:** $1.194615 (Jev $0.068615, Gemini $1.126000).
- **출처:** [상세](results-v1.md#문자열-검사-오채점-감사jev-재채점-2026-11-24), RUNS/followup/grader-audit-20261124/{labels.json,jev.jsonl,jev_dup.jsonl,agent_jev.py,jev_rescore.py,agent_gemini.py,gemini/correctness.jsonl,gemini_rescore.py}.

<a id="c02"></a>
## C02 — 구조화 답 종류 오채점 재판정

- **날짜·질문:** 2026-11-24. 시각을 묻는 과제에서 대상 일정은 맞히고 구조화 답의 종류만 `event`로 표시한 답을 코드가 실패로 세는가, 그 때문에 짝 차이 결론이 바뀌는가?
- **후보·선정 근거:** C01과 같은 Agent 원본 20개. 실패 원인이 `answer_kind`인 243건 중 정답 종류 `event_time`, 답 종류 `event`, 일정 ID 일치, 틀린 검사가 `structured_answer`(와 문자열 검사)뿐인 230건. `event` 대신 `event_set`으로 답한 13건은 실제 실패로 둔다.
- **고정 조건:** 에이전트 재실행 없음. C01의 Jev v4 행을 그대로 쓰고, C01과 같은 기준(코드 문자열 판정과 다름 또는 확률 최솟값 [0.1, 0.6))으로 고른 75건만 Gemini(`correctness-v2`)가 최종 판정한다. 규칙은 Gemini 출력을 보기 전에 정했다.
- **결과:** Gemini는 75건 중 74건에서 Jev와 같은 판정. 230건 중 229건이 성공으로 바뀌었다. 최종 성공률 test luna-index 0.708, haiku 0.677, haiku-index 0.762, gemini 0.500; dev index-on 0.723, runflow 0.576. 결론이 바뀐 짝은 dev gemini−runflow(−0.117 유의 → −0.008 [−0.081, +0.069]), dev assistant−runflow(→ −0.117 [−0.226, −0.015]), dev pattern-fixed−runflow(→ +0.034, CI 0 포함), dev haiku-index−index-on(+0.106 유의 → +0.080 [−0.008, +0.170]). test 결론은 유지됐다.
- **결정·결정자:** C01에서 개발자가 정한 채점 방식(코드 구조 검사 + Jev + Gemini)을 적용한 값이다. 이 값을 Agent 성공률의 최종 주 점수로 쓴다. ADR-0031 재검토 여부는 개발자가 정한다.
- **미비교·한계:** 재판정은 실행 뒤에 했고 사람 대조는 없다. 75건 밖 155건은 Jev만 썼다. Agent가 종류를 `event`로 표시하는 출력 형식 문제는 고치지 않았다.
- **비용:** $0.103290 (Gemini).
- **출처:** [상세](results-v1.md#구조화-답-종류-오채점-재판정2026-11-24), RUNS/followup/grader-audit-20261124/{kind_regrade.py,gemini_kind/correctness.jsonl}.

## 공통 설계 한계

- M01/A01은 실제 기준이 된 색인 켬이 아니라 색인 끔 luna에 한 요인씩 비교했다. M02는 Haiku·fixed의 dev/test와 plan의 test 조합만 보완했다. Google 2종과 plan dev는 비용 조건 때문에 비교하지 못했고 모든 요인 조합을 비교한 것은 아니다.
- assistant_v0는 읽기 전용이므로 쓰기 과제가 있는 동일 과제 비교는 같은 능력 범위를 뜻하지 않는다.
- dev의 다중 비교, 결과를 본 뒤 후보를 고른 과정, test 중단·재실행·후속 재사용 때문에 선택 편향이 있다. 새 test 비교를 최초 일회 확인이라고 부르지 않는다.
- 실험마다 코드 SHA가 다르다. 지문이 같아도 dirty 코드의 실제 바이트나 평가 수명 방어 변경을 모두 나타내지 않는다. 단일 요인 인과 효과로 과장하지 않는다.
- 상한 때문에 중단·추정 때문에 일부 과제를 미리 본 횟수, 비용 미상 호출과 임베딩 비용을 분리한다. 완료된 에피소드 비용만으로 총 청구액이나 정지한 호출 상태를 복원하지 않는다.
- Judge의 보정 표본 재사용·라벨러1명·작은 유형 표본·κ 구간 하한과 도구 목록 부재, 코드 추출기 한계를 함께 보고한다. 근거성·엄격 성공률은 보조이며 기존 성공률이나 채택 결정을 자동으로 대체하지 않는다.

<a id="source-ledger"></a>
## 원본 대조표

아래는 이 문서의 역사 기록을 정리할 때 읽은 원본의 코드·지문과 전체 파일 SHA-256이다. 파일 자체는 합성 원문·개별 판정을 포함해 git에서 제외한다. 실행 코드는 고유하게 식별되는 12자 SHA로 줄였으며 전체 40자는 원본 `config.git_sha`에서 확인할 수 있다. 행에 코드·지문이 없으면 임의로 추정하지 않는다. `MAIN_RUNS/`의 초기 원본은 실행 worktree의 `RUNS/`와 위치가 다르다.

| 원본 | 행 수 | 실행 코드 SHA | 설정 지문 | 원본 SHA-256 |
| --- | --- | --- | --- | --- |
| `MAIN_RUNS/pilot/A.estimate.jsonl` | 5 | f2e2d2be350a | 8bad39863d58f2a4682ff58fef0ae14398b6dda7d040c2ebb36c63a6d648324c | `d8d3627790bd1827021621069f9e5919439f45d0189ceea53dd4072d08e05880` |
| `MAIN_RUNS/pilot/A.jsonl` | 108 | f2e2d2be350a | bd56f9ba16dccaf4dd2d96464ac1d7a08d11a7f5726b7b0a0140d52e0193d2bb | `95567c4e92085b8d9c95f8fcae7a2035d8da7dda3cd5bdf3c93aa464747cd8fd` |
| `MAIN_RUNS/pilot/B.estimate.jsonl` | 5 | f2e2d2be350a | 7b8e466337ec05f38fd14a8c435fde6b2aae9367cea19f8250090fe05eff945b | `0c3911299399575f2de4665ea457b6e78459c72e82bbe6f01dd62eb9fafdd175` |
| `MAIN_RUNS/pilot/B.jsonl` | 108 | f2e2d2be350a | 9440475a2e8d2e1e951566d28b47c2a7adacbcd7bd2777c0d003ac266ddbb8c7 | `3999b507ce88617dbbc889bbd77b80d6d985bd3b8c46dfaceb4bccf1d43ee518` |
| `MAIN_RUNS/pilot/C.estimate.jsonl` | 5 | f2e2d2be350a | 434c5f101d18fe8aa00dedcd350bfeeed12c99c614c732629d454ba51dc7b9ab | `2b61cc99f5a35507a52169437af03ee6c5f15ba736f9465cc8baff992574c660` |
| `MAIN_RUNS/pilot/C.jsonl` | 108 | f2e2d2be350a | b51e86a8851143165b9d3bc5a05ef7bac00dd404283998daaaa4364699d27ba6 | `4e1b3c20b7ee480872f3e455aaa765e2b3f42050b8b7d9179485e53f34d3a1b7` |
| `MAIN_RUNS/regress/injection-luna-low-2.jsonl` | 6 | 출처 확인 필요 | 출처 확인 필요 | `5e292ab3d17dbcf0ef1eaccc17fb3da3f101cf5b311534afcb791d54c1346034` |
| `MAIN_RUNS/regress/injection-luna-low.jsonl` | 6 | 출처 확인 필요 | 출처 확인 필요 | `9ad3ba6cee3003cb0b82f0973e3579c8c63e2be860f8ff74e392774a8be4ca9c` |
| `MAIN_RUNS/regress/injection-terra-low.jsonl` | 6 | 출처 확인 필요 | 출처 확인 필요 | `a9a8dc45e0827940e3386ca3e4f63e9eef14a84abe5dfe00d2d4b3b46ff4bca6` |
| `MAIN_RUNS/regress/sep-luna-low.jsonl` | 36 | 121732fc0cd5 | d570cbee2302c6b3a2ad18116f5fe0d08eddd4c78cf606880b7d0ca463d064c8 | `f8dbcc6a3816ba57a3076f80e355ca971e1dd7ccfc6c3de9b33f6bc392a95f16` |
| `MAIN_RUNS/regress/sep2-luna-low.jsonl` | 36 | 121732fc0cd5 | d570cbee2302c6b3a2ad18116f5fe0d08eddd4c78cf606880b7d0ca463d064c8 | `d27feefe88ebd5368f8261608872bee827d41af96245e5c6735e7f3328dac54c` |
| `MAIN_RUNS/regress/stage7-luna-low.jsonl` | 36 | 867772e3714b | 29918a30d6f3e6ff87ac7b137f1a020979a91ddd113ae368d7b471b4fab60abe | `a1207e85dad6b0b0f774ba391924fc5d0fe49f6934b348c9229b1581cfacc775` |
| `RUNS/eval-1/assistant.jsonl` | 36 | eaaa1f7a5212 | 2de280b410cffb4e0a589fe3367d52e809711994de3769351a1ed2634cfa8cc3 | `496bc44b8f76fe0ab4ebddedc2ef481b83d2cb8689f923dfe756948195b093f1` |
| `RUNS/eval-1/runflow.estimate.jsonl` | 5 | eaaa1f7a5212 | 1a7fd00f9dbb530e2482b23bec286fd37890d4ad74db50999561a96d8c8bde99 | `c052d7ee86bdc4e1aa90bdb369531ac352319290e39c5a97cc70f50e2797f95d` |
| `RUNS/eval-1/runflow.jsonl` | 36 | eaaa1f7a5212 | a41709a321a3432ac64eae7f59a20ac481cb31293efb28b167e522b56bd82eb5 | `195eecee140d7f177332fd6272f9de533c9f9109e7404f37e3af07bdd9ac7ae7` |
| `RUNS/eval-2/assistant.jsonl` | 36 | e4f6f313baa5 | f81904d3ee6d1f411dce7972b039f719cf5914dc84d80d9ba9a4a0b725f9a037 | `d2708fae62358a85311ac5651923b79380e3d392dbda30572fa4fc35c43124b4` |
| `RUNS/eval-2/runflow.jsonl` | 36 | e4f6f313baa5 | 06e3acf95bf3ba95186dfc46f49a4de5e9306355f15ecc157b5d352e353ac569 | `108a745272c711bd998c712b9b1fa05759fa5f1695455137b87c35328e4a796a` |
| `RUNS/ablation-1/assistant.jsonl` | 108 | 2ffa079e0563 | d34f831a052f6edf209b70463cd00e642ce63072ebeaf449453dde11bac2837c | `b7987d248ee1ff9f828ae579cfac9069aead4d8bf04d9a75d4d744244c55c5d5` |
| `RUNS/ablation-1/base.jsonl` | 108 | 2ffa079e0563 | dadf4fc9ef9ef103c6fbdbe64fd657b4172bda0c863a85372b923d2c3fefaa1c | `5a8e923258400537062573a9d9dad7b194f89dc2becc05f48877313f39190ec2` |
| `RUNS/ablation-1/both.jsonl` | 108 | 2ffa079e0563 | 691fd41f7e8087a1d70af1a793ee86bdbbeefeed070df49f852bfa17a27148f6 | `86d88dfd017d6d96cde7706e8da36af090a22d821c826e0f29ab94a755068c12` |
| `RUNS/ablation-1/facts.jsonl` | 108 | 2ffa079e0563 | ada83e13d11a153ed8f0cc088c5b1371e870b7cdd97297e4fbd3c1c0eeeccd27 | `d79c906ffc6dbd1ef6fb6011b442674a1414ed4319125b1dd6517debfd699436` |
| `RUNS/ablation-1/reasoning.jsonl` | 108 | 2ffa079e0563 | 056bcefc584eb71610347b553ea22458ab322790564308045f72c835d93dbb3d | `fdd94e5530f31a7ab289ab57070eda2887f66aed55288ac5cf0f4b63bf401415` |
| `RUNS/baseline-v1/assistant.jsonl` | 264 | fc4a577ff856 | a6b55d8f6756fbd7a2af28744e4129a5043e07895bae1d19e082b7dffe57e3a5 | `01e03c203d64a55073c4ba5a128bda8f1695414b304fc3bc72f67d23e7527a89` |
| `RUNS/baseline-v1/runflow.jsonl` | 264 | fc4a577ff856 | cc4e254546525a78c1f8a8b4ed77ddba2d634fcab323360d13e34a534be6b883 | `1d36de07b8d37fed4ed7b2ea13697b58df7d4f8578b56ee7be7d6a3b3565fa1c` |
| `RUNS/models-v1/gemini.estimate.jsonl` | 4 | fc4a577ff856 | 2e1e976b6746d4b6a3b91af62da364abb58794cf50926ba4002ae5e0c00c353b | `8de8dfa336de39b5a799ce26cd9d50d68f9447a19e87d3ee473fc77315b10525` |
| `RUNS/models-v1/gemini.jsonl` | 264 | 2c0428542c39 | 84065f9ee20b59e3a58bee3db4f46f77bb704a23b7e6a390e218c43c43f99a7b | `c938a5135ee3c237cf4ab0c4526b59283dd24ce85c75e50a70398aa7e0a36371` |
| `RUNS/models-v1/haiku-est.estimate.jsonl` | 4 | 2c0428542c39 | f0e71fd987fad6e4f6d86867475a62f4284a3a1e4e9ae45f01337b807b74733a | `2b9da48f82c6e4ea3e6220c99b7d2603ae2aeae83e35340015858027fc5f3648` |
| `RUNS/models-v1/haiku.estimate.jsonl` | 4 | fc4a577ff856 | c875eb7958a13a576d7669957fde76fe229f746d3c8dbe0d90ed328738b1df6e | `1cbefe6670fdecc87d5933055bf29d90725938c0143c275c51bcbbabb9238108` |
| `RUNS/models-v1/haiku.jsonl` | 264 | 2c0428542c39 | 8079350d46392f6180d6fcf89ca8ba5ecb29c8ba66667ecd8cfde199bff96633 | `413886c4ae7c4d3d65adbcb4dbb90d91a77dc167a46119bc734db1a602b5fa7e` |
| `RUNS/compare-v1/index-on-low.estimate.jsonl` | 8 | c627a789ce94 | d03ebb229cb2f2ae3b54830e5cd5cf55fc589adf9bb08463d579cc370cab6d28 | `ecaa8c0cc3697e5eebde02f518d1ef4c250d0cc2c0b48909cb171877192fac9e` |
| `RUNS/compare-v1/index-on-low.jsonl` | 264 | c627a789ce94 | f886d6ed7c7b5ca349ba4709fbfab28d12942f35e824230616aa690398eb2c6e | `a774a42d28fefcee786a1dd86a6016060cf872fe44e290cf900393f627fb1126` |
| `RUNS/compare-v1/index-on.jsonl` | 264 | fda3bf3ed748 | c5b09f479a94e5b8b8a93154c1484a74ff9040b3bcd2acdd008f289faf26302a | `5264d7cba29aced0b4e2e0f008d94e8926f71f75596f5547094a5eae400ec0d8` |
| `RUNS/compare-v1/pattern-fixed.jsonl` | 264 | fda3bf3ed748 | 0b1ac18fafd5d91c0f7c26a9ae8e4ae9b6b64cd97c5135e62f1a31925dfb4852 | `1886ce8db4f1e7b58ec82c59563cd81a0b49d5e905cd621354ee9f71f19616cf` |
| `RUNS/compare-v1/pattern-plan.jsonl` | 264 | fda3bf3ed748 | 677d03a03d9b3ff5638da56e08cf35453cca0070df2d159bc023e607935a4d6e | `adab750599b038ce08b84ffc089648928b5323b363d95c01e0af0df8a60d2310` |
| `RUNS/test-v1/assistant.jsonl` | 130 | fda3bf3ed748 | 30dfe2d13221bd5654e0ca226fc7db1225154fae37884cf0867d74c374704cf4 | `797ba315bd2fe461ad94a75c26dbe3bed09215c30e8b8e0284a755305e59b5a5` |
| `RUNS/test-v1/gemini.jsonl` | 130 | 2c0428542c39 | c682835973991c97138bcc7870711e1bd828515876826f13714235a45f4ac4dd | `5604cb9dd029f0e72c630ff862c36e2fc7d12b1d4c616fa34375de5decc8a5ab` |
| `RUNS/test-v1/haiku.jsonl` | 130 | 2c0428542c39 | 4c66f201a5e1da2ed4591ba8b7994fed5a8955c2c899c36ceb229290bef28a62 | `fc53397f04801ea4a304b82742cea860b3453b68ed244658baaba5a3af4dc92c` |
| `RUNS/test-v1/luna-fixed.estimate.jsonl` | 8 | c627a789ce94 | 9bdb60e36f164cced5b7bc1644c16580daf95d7d58a5be6fc297968282c522bd | `4ae1b6158de4f4dd678c86b6641f0daf6115b5239e72687a16e690a30afae121` |
| `RUNS/test-v1/luna-fixed.jsonl` | 130 | c627a789ce94 | d20ec86ce70b31c0c73c70f4962cf84233c66d141b9d151ab7be60fdc0b034cc | `bd5d987034373e381e0eef4fa2cc7bbb576414ea4d0bc53b36cb56486cac4f81` |
| `RUNS/test-v1/luna-index.jsonl` | 130 | fda3bf3ed748 | db2a048617122f6fefe03d5605adb0e70e423c74eda1cbf46b8bcbb0b11c55d8 | `261dfd10fd9f1e4bec655ae2c25be944a570df63bb3a0475620c810cc8ea946c` |
| `RUNS/test-v1/luna-plan.jsonl` | 130 | fda3bf3ed748 | 6c75a91af5cb2d4dc1fda15f1428d723c316f733e29929ef9d671bd0864b2cac | `e6c095f9ceb7c66537933ac1dddd590286c5d5a24507d05a1385a95dc5e12738` |
| `RUNS/test-v1/luna-react.jsonl` | 130 | fda3bf3ed748 | c86b7a3587acebf3489dd87092f69646cdfc5a58c824a15b2b82a23d33435f36 | `4fc090368c6e28edf6df87349e938aa001c1dd94c0d61f90679841921f5adc83` |
| `RUNS/baseline-v1-aborted/assistant.estimate.jsonl` | 8 | 205e3557b315 | a3b828956d0309c8cc631fcef43dc4818c60e743017e8142745cc8d6902c09cb | `130504bdc25b17cf14540a7100780898a632f01f47fabb9261e421deaf2ed775` |
| `RUNS/baseline-v1-aborted/assistant.jsonl` | 264 | 205e3557b315 | 5cf94a3019b9f59d5c400325098a4485563e4cb82aa24e63defa2b530a49f432 | `690f9293d1467ec6747aaf1ef863a40ec42bac33bdbe905318c52579da5d47d4` |
| `RUNS/baseline-v1-aborted/runflow.estimate.jsonl` | 8 | 205e3557b315 | ce7842775617b3880f945fb55c54e3574cb413d6b2fedc9ad193780ff944e5da | `e13240bd3af66593077effb9aaa2172deff93a59b75e48fc32f1c85e14c86237` |
| `RUNS/baseline-v1-aborted/runflow.jsonl` | 191 | 205e3557b315 | 120860c5e90f2b9b684727c4081c4f892210eeaacb04f6629d9255ffcbba9582 | `3cab35b3caef1f8e60aae370fd09955962b13b749615de3b9405853033898c8d` |
| `RUNS/crashed/gemini.jsonl` | 0 | 출처 확인 필요 | 출처 확인 필요 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `RUNS/crashed/index-on.jsonl` | 22 | fda3bf3ed748 | c5b09f479a94e5b8b8a93154c1484a74ff9040b3bcd2acdd008f289faf26302a | `bb6f9e5b887e76d254aa767edc28c81e36238f60e045dce3993a37a1d330975e` |
| `RUNS/crashed/pattern-plan.jsonl` | 0 | 출처 확인 필요 | 출처 확인 필요 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `RUNS/crashed/test-luna-fixed-hung.jsonl` | 4 | fda3bf3ed748 | f7111b52463806af295b9ba5ed1a191f60869faf5e4def4cda30c5b9be283a3b | `d01e17895fe8825e01658b99b7ca4cb35c705439c15c2ac1913632762f62022c` |
| `RUNS/crashed/test-luna-fixed-hung2.jsonl` | 9 | fda3bf3ed748 | f7111b52463806af295b9ba5ed1a191f60869faf5e4def4cda30c5b9be283a3b | `4f7f0fe6434f7135ed1f6a12b613e927d27ed148d2f768443b5001e8dcbdb73f` |
| `RUNS/crashed/test-luna-fixed-partial.jsonl` | 67 | fda3bf3ed748 | f7111b52463806af295b9ba5ed1a191f60869faf5e4def4cda30c5b9be283a3b | `30c4bc50d016cadbd038aab9d46535afeb94ca1fc50623e83470bbb8f9eb6886` |

### 주요 집계·생성 출처 해시

| 출처 | SHA-256 |
| --- | --- |
| `RUNS/retrieval/dev-openai-v1.json` | `24879f386159a53eff596125f6c38fd6ac41c92a85c2f3fd14596062af44b9d0` |
| `RUNS/retrieval/test-openai-v1.json` | `12754a47ac6b77fe1cb66c6a1ab0d0e6e27d20284b20a7b63f2bb0ec845db820` |
| `RUNS/judge/agreement-ab.json` | `9267ef8de3240b1317023c6fb7389a24ea19d96f4f5f9198cc6b9751344dab12` |
| `RUNS/judge/agreement-human.json` | `48e04f7d78202cecff643f7b1d6a2cee8b0a7638dc27b07a499579f21276c5a6` |
| `RUNS/judge/agreement-strong60.json` | `5ef86273fc0792b768a2fa210d51d3b730c48289d4cc52365d7cfbf8f5a9c7aa` |
| `RUNS/judge/blind60-summary.json` | `99681fb4a98b31e688f31c3c613f0794626dd44340950c9fb31273779702f565` |
| `RUNS/judge/v3-blind60-set/blind60-summary.json` | `f06589d727bdea1dccb2f1ec77828803a55260bfc269ed2314b817f14b1a43e5` |
| `RUNS/judge/v3-blind60-set/human-agreement.json` | `bc873227788328336a4dd2b0a6a5e49bfa21b373e47b1bfa0acce9badfa1e116` |
| `RUNS/handoff/overnight-plan.md` | `bf4dcc2ef4d638953710dc20dbc9c81e41f7b38212b122da80161af879c932d0` |
| `eval/datasets/orbit-eval/v1/manifest.json` | `93bb5c70ea51ebb784b93e52b739f4e6f6eab9bba7c184c61ae8b8d99ea7f5c8` |
| `eval/datasets/orbit-eval/v1/realize-manifest.json` | `74c0f1200dbcdc4506865385314004f903b65202c2c604dac6cc78414275344a` |
| `eval/datasets/orbit-eval/v1/auto-review-manifest.json` | `0c35b80fe08ac64c494f8a8c3baae065e665d3cd364dbcb7a9fff728a54ef319` |
| `eval/datasets/orbit-eval/v1/search-manifest.json` | `021118326256bd5ed98b43a54a26e22a0caa7c12f3043ff571c1732ee4b7d1c4` |
