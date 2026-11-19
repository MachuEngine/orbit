# 메일 RAG 패턴 비교 v1

설계: [ADR-0035](../adr/ADR-0035-rag-pattern-evaluation.md). 현재 생성 모델·에이전트·검색의 역할을 분리해 검색 시점·반복 정책과 질의 재작성 효과를 측정한다. 제품 기준 설정은 변경하지 않는다.

## 비교 설정

| 구성 항목 | R0 기준 | R1 | R2 |
| --- | --- | --- | --- |
| 생성 모델 | `openai/gpt-6-luna` | 동일 | 동일 |
| 추론 강도 | `none` | 동일 | 동일 |
| 에이전트 패턴 | ReAct | ReAct | ReAct |
| RAG 패턴 | 도구 호출형 RAG | Retrieve-once + ReAct | Rewrite → Retrieve-once + ReAct |
| 색인 검색 시점 | 모델이 결정 | 사용자 턴 시작 | 질의 재작성 뒤 사용자 턴 시작 |
| 색인 검색 횟수 | 모델이 결정 | 턴당 1회 | 턴당 1회 |
| 질의 | 모델이 선택 | 현재 질문 앞 500자 | 같은 모델의 재작성 1회. JSON 형식 실패·빈 값·길이 초과·출력 절단 시 현재 질문 앞 500자로 fallback |
| 검색 엔진 | Vector + Kiwi/`ts_rank`, RRF k=60 | 동일 | 동일 |
| 반환 수 | 기본 5·모델이 최대 20 선택 | 5통 | 5통 |
| 임베딩 | `text-embedding-3-small`, 1,536차원 | 동일 | 동일 |
| 청크 | 800자·overlap 100자 | 동일 | 동일 |
| 추가 reranker | 없음 | 없음 | 없음 |

R1·R2는 색인 검색 정책만 턴당 한 번으로 제한한 근사이며, 전체 앱을 단일 응답 생성 체인으로 바꾼 Naive RAG가 아니다. 기존 메일 문법 검색·메일 본문 읽기·다른 영역 도구·승인·쓰기는 ReAct를 유지한다. R0→R1은 검색 시점·질의·호출 수·반환 수 선택을 묶어 바꾼 패턴 비교다. R1→R2만 같은 1회 정책에서 질의 재작성의 추가 효과를 비교한다.

## 표본과 사전 규칙

- 주 비교: 검수 후 `scenario:mail_fact`인 **dev 21 tasks·17 clusters**, 단일 턴 16개·다중 턴 5개. 각 설정 3회, seed 0 → **63 episodes/설정**.
- 스모크: 유효 dev **21 tasks·21 clusters**, 각 설정 1회. 주 비교와 겹치는 7과제를 독립 표본으로 합산하지 않는다.
- 지표: task success, pass³, R1−R0·R2−R0·R2−R1 paired difference와 cluster bootstrap 95% CI(10,000회·seed 0). 실패 유형·검색/LLM 호출·fallback·지연·비용은 보조 지표다.
- R0도 같은 코드·현재 시점에서 다시 실행한다. 과거 dev 전체 64.4%나 test 64.6%를 이번 21과제 부분집합의 기준선으로 쓰지 않는다.
- 새 test 실행과 Grounding Judge 호출은 없다. 기존 검색 단독 dev 결과는 [이전 검색 평가](results-v1.md#검색-세트-dev-결과)에서 확인할 수 있지만, 고정 질의 retrieval 점수와 이번 동적 종단 지표를 같은 수치로 취급하지 않는다.
- 전체 LLM+임베딩 기록 비용 상한 $1. 먼저 색인 dry-run, 설정별 estimate 8건을 수행한다. 비용 미상·추정 초과·600초 무진행이면 중단한다. 코드 SHA·선정 집합·스크립트/단가/manifest 해시를 고정해 재개한다.

## 측정 결과

<!-- RAG_RESULTS_START -->

실행 코드 SHA: `5a03946b96c1981debf651545c44341c240a3d61`. 모든 설정을 같은 커밋에서 실행했고 `git_dirty=false`다. test 사용 0회.


### 주 비교: 메일 dev 21과제 × 3회 · 17 clusters


| RAG 패턴 | 성공/episodes | 성공률 [95% CI] | R0 대비 짝 차이 [95% CI] | pass³ | 본 실행 LLM 비용 | 지연 p50/p95 |
| --- | --- | --- | --- | --- | --- | --- |
| R0 · 도구 호출형 | 48/63 | 76.2% [61.1, 88.3] | 기준 | 52.4% | $0.07092652 | 10.00 / 21.50초 |
| R1 · Retrieve-once | 47/63 | 74.6% [55.6, 88.9] | -1.6 pp [-14.0, +9.1] | 57.1% | $0.06116013 | 7.93 / 21.75초 |
| R2 · Rewrite → Retrieve-once | 51/63 | 81.0% [63.3, 93.9] | +4.8 pp [-3.3, +12.3] | 71.4% | $0.03728110 | 8.70 / 24.33초 |


**R2−R1(질의 재작성의 추가 효과):** +6.3 pp [-1.5, +15.8].


**판단:** 기준 대비 양의 paired CI를 보인 후보가 없어 기준 설정을 유지한다. CI가 0을 포함하는 비교를 동등성 입증으로 해석하지 않는다.


### 검색·재작성 동작 관찰


| 패턴 | 색인 도구 기록 | 서버 초기 검색 | 검색 오류 | 0건 결과 | 재작성 기록 / fallback | 질의 절단 |
| --- | --- | --- | --- | --- | --- | --- |
| R0 · 도구 호출형 | 63 | 0 | 0 | 0 | 0 / 0 | 0 |
| R1 · Retrieve-once | 84 | 84 | 0 | 0 | 0 / 0 | 0 |
| R2 · Rewrite → Retrieve-once | 84 | 84 | 0 | 0 | 84 / 6 | 0 |


검색 횟수는 턴별 기록 합계로 episode 수와 다르다. 오류 거절도 도구 기록에 포함되고 실제 성공 검색 수와는 구분한다. 이 지표로 정답 근거 Recall이나 환각률을 추정하지 않는다.


### 스모크: 21과제 × 1회 · 별도 집계


| 패턴 | 성공/21 | 성공률 [95% CI] | R0 대비 짝 차이 [95% CI] |
| --- | --- | --- | --- |
| R0 · 도구 호출형 | 13/21 | 61.9% [42.9, 81.0] | 기준 |
| R1 · Retrieve-once | 13/21 | 61.9% [42.9, 81.0] | +0.0 pp [-14.3, +14.3] |
| R2 · Rewrite → Retrieve-once | 15/21 | 71.4% [52.4, 90.5] | +9.5 pp [-9.5, +28.6] |


주 비교와 겹치는 7과제는 따로 표기했으며 이 표를 합산해 독립 표본을 늘리지 않았다. 스모크는 채택 근거가 아닌 기능 퇴행 관찰용이다.


### 실행별 비용 · 사전 추정 포함


| 실행 | episodes | LLM USD | 임베딩 USD |
| --- | --- | --- | --- |
| estimate-agentic | 8 | 0.00870067 | 0.00000286 |
| estimate-retrieve_once | 8 | 0.00717666 | 0.00000824 |
| estimate-rewrite_retrieve_once | 8 | 0.00433109 | 0.00000532 |
| mail-agentic | 63 | 0.07092652 | 0.00001502 |
| mail-retrieve_once | 63 | 0.06116013 | 0.00001610 |
| mail-rewrite_retrieve_once | 63 | 0.03728110 | 0.00005730 |
| smoke-agentic | 21 | 0.01611018 | 0.00000422 |
| smoke-retrieve_once | 21 | 0.01297999 | 0.00001068 |
| smoke-rewrite_retrieve_once | 21 | 0.00974704 | 0.00001080 |


**합계: $0.22854392**. 비용 미상 0건. 단가는 [OpenRouter models API](https://openrouter.ai/api/v1/models)의 2026-11-19 스냅샷과 같은 표준 단가로 설정했다(입력/캐시/출력 1M tokens당 $0.10/$0.01/$0.50). 임베딩은 $0.02/1M tokens다. 실제 청구액 대조는 아니다.


### 설정 지문과 원본 출처


| 집합 | 패턴 | 설정 지문 | 결과 원본 |
| --- | --- | --- | --- |
| mail | R0 · 도구 호출형 | `c1dd2ac1319147f8bf380362ca95fe924369dc7354c8f7634157a90e5b39115b` | `runs/rag-patterns-v1/mail-agentic.jsonl` |
| mail | R1 · Retrieve-once | `708d139575e8168980bb9b96ab71244cdd66ed7e761eeabaf66215356dc5d5aa` | `runs/rag-patterns-v1/mail-retrieve_once.jsonl` |
| mail | R2 · Rewrite → Retrieve-once | `c6e750693f29fcaf5033e7160601fa312d28801d43f12deabde3b4b9a058111a` | `runs/rag-patterns-v1/mail-rewrite_retrieve_once.jsonl` |
| smoke | R0 · 도구 호출형 | `b2c62764f06ba6c6db9c7a506d92868ea9caeae1ab2f53290cab95e78a0b8654` | `runs/rag-patterns-v1/smoke-agentic.jsonl` |
| smoke | R1 · Retrieve-once | `9f7fea6032107ab10ff9b07d975ca9babe27abe6e4da1732fdfaa6b4ee679921` | `runs/rag-patterns-v1/smoke-retrieve_once.jsonl` |
| smoke | R2 · Rewrite → Retrieve-once | `0e934d3cc16377d0e6cbfd9416127582a36e3fca7d52eff518ac78563ed7e0af` | `runs/rag-patterns-v1/smoke-rewrite_retrieve_once.jsonl` |


각 `*.stats.json`에 기존 보조 지표 전 열, `report.json`에 검색/fallback 진단과 비용·해시를 보존했다. `frozen-study.json`에는 선정 task·cluster·코드·단가·스크립트·manifest 해시가 있다. 원문과 개별 판정은 git 제외다.


| 파일 · RUNS 기준 | SHA-256 |
| --- | --- |
| `estimate-agentic.estimate.jsonl` | `4a2ff9663afc3a7f9d887ee251beba8bde52dd3785376cc3d30ef41797c43651` |
| `estimate-agentic.status.json` | `9e2746eb27b4ffef6a85e82d7c48b979cee362f3cd8e6bfac7959cb113e75059` |
| `estimate-agentic.embedding.json` | `356db0e68d487e38eabdc6a22a0095c51faf9d795ff89c1020018825f514a11f` |
| `estimate-retrieve_once.estimate.jsonl` | `00231959c3214bf4e173f4517ca2e2f5790832e1c494a99a183e90825430ec85` |
| `estimate-retrieve_once.status.json` | `54ba0ae781355af0c8268c041496467bc9e9af6e8f67b9adb253735d1c86e06e` |
| `estimate-retrieve_once.embedding.json` | `5eff7faa019a761a5ac2b5043637a162e5464fab359f22d7cb1a0dda4cc73544` |
| `estimate-rewrite_retrieve_once.estimate.jsonl` | `8dc77d1e9ba7b56f2da9d99ada2ed60c4041c402a7035e663283c7782d686caf` |
| `estimate-rewrite_retrieve_once.status.json` | `69b545b131090dcadd437b34ce693d84a30d97cf98c90b2bf6e2af4a24bef827` |
| `estimate-rewrite_retrieve_once.embedding.json` | `07278d65d8ef7b40fcb2e74b13565152901caf8d2df9de6eb128a35b8efa5eda` |
| `mail-agentic.jsonl` | `beb1ba1f8aac016a618e71dd3bf2a1e2cfb6e804a3c88ffbe456719c75215c5c` |
| `mail-agentic.status.json` | `4f8dbbc0d87aea9a5877cac3031a2da4ee4d95db9513eab99b2f0e81b1528871` |
| `mail-agentic.embedding.json` | `af29f6d121a12e6a8d4bd75ee84e3138bd19c1f9ccf11ff9bcf1039934ac2319` |
| `mail-agentic.stats.json` | `c57793209c7afa86183decd8b9cea76e6676895ba72376661b7d20718c59e47f` |
| `mail-retrieve_once.jsonl` | `43923f72a8e5d0c5991a94a7e53cd769b7c257c7197f6e7f824f82551e19c93f` |
| `mail-retrieve_once.status.json` | `f664c7f7c2c05d24cbee97898d993dbf2005b58374000d3a080e4631c7521735` |
| `mail-retrieve_once.embedding.json` | `9270c9b1fcae6c2d794e17b5d21e915170ecdaac61c1fcf3cd9f07e68a805f16` |
| `mail-retrieve_once.stats.json` | `07069f3adfc60c25ba284aef4d47927cc140d9e696045de961d6cd715beae2ff` |
| `mail-rewrite_retrieve_once.jsonl` | `122ba9d2cad6b218c08e73afa9671ff1314aaeffaade273d94e6b7a46fc12937` |
| `mail-rewrite_retrieve_once.status.json` | `3bf98a5588cb74ab5b5d565b938dacd2aa76c6a309c9a5aa9137bf91dd89dff3` |
| `mail-rewrite_retrieve_once.embedding.json` | `23d682626a230aff4c7aeda45f6f5ef8e624caba5efc01c2c9bca251e4725be0` |
| `mail-rewrite_retrieve_once.stats.json` | `045968fe2875ce8a5f957b53fada809a33de1429486450ae5bc64ff27efa61be` |
| `smoke-agentic.jsonl` | `58e18b579d336b89f7926afd8edc6768fe23a259dd29f55c6a2900bef1827228` |
| `smoke-agentic.status.json` | `b1bc92a69ecb45caa1d9e4f320cf1d881c4eb2c96c648c7e1de3dd6b715fc7b1` |
| `smoke-agentic.embedding.json` | `ef393653cd84fa97142afe8a98c181a9f2e49f37c599cd0ce8d819121a110b03` |
| `smoke-agentic.stats.json` | `129236d614dccacc5c5832c64323304d39bfbbb6d4e7b34a1345f56e3fc95caa` |
| `smoke-retrieve_once.jsonl` | `d49ebfc4b23487658cbab5e4fec496aef9d12e23a2abf1fa85f219c33c4cbcd0` |
| `smoke-retrieve_once.status.json` | `3f748ea5a7441bba2af92f98046c9a2607df23839ced1230e42594d707fdd876` |
| `smoke-retrieve_once.embedding.json` | `fc50640e205348b8f46dd6784e69b7e4f67dea4900f3a895e102cdd4d592d935` |
| `smoke-retrieve_once.stats.json` | `c91aa076dba32b3ff836da537164784396b3dbddcbab17bfc889b120d30cf228` |
| `smoke-rewrite_retrieve_once.jsonl` | `42790c4a25f88e4b2d94ae62f951360eb57dfa35ee7f60b4b6f3225c34ea05b0` |
| `smoke-rewrite_retrieve_once.status.json` | `b2b20a2430381e52653d502a9850dc67e960ed940784602482754107c225dd22` |
| `smoke-rewrite_retrieve_once.embedding.json` | `4ce0f559b92de5ea7dd11b9ada491e3a13560f4087d3f37a4f288702ed5da8b3` |
| `smoke-rewrite_retrieve_once.stats.json` | `44344bd1f4f346ec4c782ebbc58e34da89ce641a3b11ae226f4b4d5fec304815` |

<!-- RAG_RESULTS_END -->

## 보조 지표의 관찰

| RAG 패턴 | 다중 턴 성공 | wrong_answer | unexpected_state_change | wrong_state_change | 평균 도구 호출/episode |
| --- | --- | --- | --- | --- | --- |
| R0 · 도구 호출형 | 13/15 | 13 | 1 | 1 | 2.41 |
| R1 · Retrieve-once | 10/15 | 11 | 3 | 2 | 2.73 |
| R2 · Rewrite → Retrieve-once | 12/15 | 9 | 3 | 0 | 2.52 |

다중 턴은 5과제 × 3회이며 마지막 턴까지 맞아야 성공한다. 실패 유형은 공식 채점 분류이며 근본 원인을 확정한 값이 아니다. 작은 하위 표본의 수치를 패턴 우열로 일반화하지 않는다.

R2의 주 비교는 84회 재작성 중 6회(7.1%), 스모크는 24회 중 1회(4.2%) 원 질문으로 fallback했다. 이 실행 정책 전체의 결과이며 매번 재작성에 성공했을 때의 순수 효과가 아니다. 현재 trace는 fallback 여부만 보존해 형식 오류·빈 값·길이·출력 절단별 원인은 분해하지 못한다. 재작성 전송 오류는 0건이다.

미채점·짝 누락·모델 불일치·비용 미상은 모두 0건이다. 9개 실행(estimate 3개·주 비교 3개·스모크 3개)은 모두 정해진 task/repeat 집합과 코드 SHA로 완료됐고, 집계 전에 원본·임베딩·상태 해시를 대조했다.

## 해석의 한계

- 합성 메일·작은 17개 독립 묶음의 dev 탐색이다. test나 실사용 일반화, 다중 비교 보정, 운영 품질 기준 통과를 검증하지 않는다.
- 생성 모델은 고정했지만 R2는 동일 모델 호출 한 번을 추가한다. 추가 호출·지연·토큰을 포함한 시스템 구성 비교이며 생성 모델 자체의 우열이 아니다.
- R0는 검색 여부·질의·반환 수를 선택한다. R1·R2는 무관한 질문에도 초기 검색을 수행할 수 있고, 뒤의 추가 색인 검색을 막는다. 후속 메일 문법 검색 등은 여전히 가능하다.
- 구조화된 답과 상태를 포함한 공식 성공률을 유지한다. 정정 설명의 옛 값처럼 문자열 규칙에만 걸리는 답이 있어 점수는 표현에도 민감하다. 사실 근거율(코드)은 서술 근거성 Judge와 다르다.
- 질의 재작성·원문 질의가 500자를 넘을 때의 절단, 응답 형식 fallback, 전송 오류·중단을 구분한다. 오류와 불확실한 호출을 정상 fallback이나 비용 $0으로 숨기지 않는다.
- 실행 순서는 각 단계 안에서 R0→R1→R2로 고정했고 캐시를 재사용했다. 관찰한 비용·지연 차이에 실행 시점·캐시 상태의 영향이 섞일 수 있어 순수한 인과효과로 해석하지 않는다.
- 색인은 계정별 고정 스냅샷·캐시 재사용이다. 실시간 동기화·새 메일 쓰기 이후 색인 갱신을 평가하지 않는다. 별도 임베딩·청크·reranker ablation은 없다.
- 비용 상한은 기록 비용과 episode 경계의 러너 통제다. 최종 episode 초과에 $0.15를 남기고 감독하지만 제공자 최종 청구액의 절대 보장은 아니다. 누락·취소 비용이 있으면 하한으로 보고한다.

## 재현과 검토

실행 스크립트는 `scripts/eval-rag-patterns.sh`와 `.py`다. 키는 원본 저장소의 `secrets/.env`를 스크립트에서 source해서만 읽는다. 필요하면 경로를 `ORBIT_EVAL_SECRETS_FILE`로 지정한다. 이전 임베딩 캐시 위치는 `ORBIT_RAG_CACHE_SOURCE`로 전달하면 자기 결과 디렉터리에 복사해 쓰므로 원본 캐시를 변경하지 않는다.

```bash
backend/.venv/bin/python scripts/eval-rag-patterns.py plan
# 아래 두 명령은 유료 실행이다. estimate gate·검토·상한을 먼저 확인한다.
bash scripts/eval-rag-patterns.sh estimate
bash scripts/eval-rag-patterns.sh run
```

실행 원본·개별 도구 결과·키는 커밋하지 않는다. `runs/rag-patterns-v1/`에 비용·명령·상태·고정 설정·해시와 집계를 남긴다. 중단된 호출은 자동 재시도하지 않고 실패 상태를 보존한다. 결과가 양의 차이를 보이더라도 기준 설정 변경은 **개발자 결정 필요**로 보고한다.

완료된 결과를 재집계할 때는 `backend/.venv/bin/python scripts/summarize-rag-patterns.py`를 쓴다(LLM·DB 호출 없음). 유료 재현은 기존 파일을 덮어쓰지 않는 별도 체크아웃에서 위 실행 코드 SHA로 해야 한다. 보고 문서의 후속 커밋과 실행 SHA를 구분한다.

이번 실행 보관본은 메인 체크아웃 기준 `../orbit-eval-archive/rag-patterns-v1/`에 별도 저장했다. 캐시와 작업 인계 메모를 제외한 실행·집계·고정 코드 65개 파일의 목록은 `SHA256SUMS`이며 그 파일의 SHA-256은 `1b32298f626b0cb75e4eefc60ed2c187cbb4ac3846fb6345c311a5ade5df3ccb`다. 기존 v1 보관본은 변경하지 않았다.
