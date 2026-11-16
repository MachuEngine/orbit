작업 명세: 평가 스위트 v1 (d2) 종단 과제·스모크 생성기 (ADR-0026 §4·§5)

작업 위치: 저장소 루트. (secrets/ 읽지 않음. 실제 LLM API 호출 금지 — 표현 변형 LLM 호출 코드는 만들되 테스트는 스크립트 LLM으로만. 실제 실행은 메인이 한다.)

## 목적
Sim 세계 v1의 시나리오 기록(`eval/datasets/orbit-eval/v1/skeleton.json`의 `scenarios[].truth`)과 세계(`world.json`)에서 종단 과제 약 240문항(test 140, dev 100)과 스모크 25문항을 결정적으로 만든다. 정답은 코드가 truth와 세계에서 계산한다. 표현 변형 파생 문항만 LLM으로 질문 문장을 바꿔 쓴다.

먼저 읽을 것: docs/adr/ADR-0026-eval-suite-v1.md(특히 §4 규모·유형별 수·문구 규칙·모호한 질문, §5 스모크, §7), ADR-0027, ADR-0010(분할·봉인·정답 지문 중복 제거), ADR-0023 §3(approval_policy), backend/src/orbit/evaldata/schema.py(직전 단계 d1에서 확장된 Task·Gold·expected_changes·time_zone·fault_plan·answer_facts_required), evaldata/dataset.py, v0 생성기(evaldata/generate.py, gold.py — 분할·중복 제거 방식), evaldata/v1/(skeleton.py truth 유형, realize_models.py·realize.py — LLM 호출·비용 상한·재개 방식 재사용), evalrunner/cli.py·task_source.py(데이터셋 로드, 표본 실행), connectors/sim/faults.py. 백엔드 규칙: .claude/skills/backend-python/SKILL.md + .claude/project-skills/backend-python.md.

## 범위
변경 가능: 새 모듈 backend/src/orbit/evaldata/v1/tasks*.py(권장: `tasks.py` 본체·CLI, `task_templates.py` 질문 문구, `task_gold.py` 정답 계산, `paraphrase.py` 표현 변형), evaldata/schema.py의 Manifest 확장(선택 필드만, v0 호환), evaldata/dataset.py 로드 보조, evalrunner/cli.py에 스모크 부분집합 선택 옵션, 테스트, eval/worlds/README.md 또는 docs/pilot/README.md 사용법.
금지: 세계 데이터(world.json·skeleton.json) 변경, 러너 채점 규칙 변경(d1에서 끝남 — 필요하면 멈추고 보고), 제품 코드, v0 데이터.

## 문항 유형과 수 (dev+test 합계, ADR-0026 §4)
각 유형의 원본 문항은 서로 다른 시나리오에서 만든다(묶음 수를 늘리려고). 사용자 6명에 고르게, LA 사용자 포함. 생성 계열(generator_family)도 유형마다 고르게.
| 유형(tag) | 수 | 시나리오 → 정답 |
| --- | --- | --- |
| calendar 일정 조회 | 32 | day_agenda(event_set, 취소 제외), free_slot(time_slot), reschedule(event_time), cancellation(취소된 일정이 빠진 그날 event_set), project_week(event_set), person_next(event), cross_midnight(event), tz_event(event_time), all_day(그날 event_set에 종일 일정 포함) |
| mail 메일 내용 질의 | 40 | mail_fact: expected_answer=message_set[latest_message], answer_facts=latest 표기, answer_facts_required=true, must_not_contain=previous 표기(이전 값이 최신 값의 부분 문자열이면 must_not_contain에서 빼고 manifest에 수를 적는다) |
| write 쓰기 | 20 | meeting_request→event_created(제안 시각·제목·참석자), event_change_target move→event_updated(start·end) / cancel→event_cancelled, reply_needed→draft_created(to=발신자, reply_to=awaiting_message). 그중 약 4문항은 approval_policy=["reject"]이고 expected_changes=[] |
| multi_turn 다중 턴 | 20대화 | 2~3턴 스크립트. 예) person_next "다음 미팅 언제야?" → "그 미팅을 30분 뒤로 옮겨줘"(event_updated), mail_fact "견적 금액 알려줘" → "그 금액 확인했다고 답장 초안 써줘"(draft_created, body_facts=금액), day_agenda "내일 일정 알려줘" → "그중 오후 일정만"(event_set). 정답은 마지막 턴 기준 최종 상태·답 |
| document 문서 질의 | 16 | doc_fact(document 답 + answer_facts_required), doc_version(최신 판 document + 최신 값, must_not_contain 이전 값) |
| reminder 리마인더 | 16 | deadline: "마감 하루 전 오전 9시에 알려줘"류 → reminder_created(due_at=사용자 시간대 계산, 과거면 다른 문구로), 그중 약 4문항은 리마인더를 요청하지 않는 마감 질문이고 expected_changes=[](요청 없을 때 미생성) + 구조화 답 없이 answer_facts로 마감일 |
| mismatch 메일·캘린더 불일치 | 16 | mismatch: event_time(캘린더 값), must_not_contain 메일 값 |
| unanswerable 답할 수 없는 질문 | 16 | absent: abstain |
| ambiguous 모호한 질문 | 16 | ambiguous: clarify(readings=해석별 정답) |
| fault 커넥터 오류 파생 | 24 | 위 원본 중에서 골라 fault_plan을 붙인다. 읽기 과제: 첫 조회 호출에 transient 또는 rate_limited(같은 정답 기대). 쓰기 과제: result_unknown(적용됨) → 같은 expected_changes(중복 생성 없음, D5-5). 원본과 같은 묶음 |
| paraphrase 표현 변형 파생 | 24 | 원본 질문을 LLM이 뜻을 유지해 다시 씀. 같은 정답·같은 묶음. 다중 턴·모호한 질문은 제외 |

- 모든 과제: `time_zone`=계정 사용자 시간대, `as_of_time`=시나리오 as_of(없으면 스냅샷 이후 첫 평일 오전 계산), `account_id`, `scenario_id`, `tags`(유형 + 시나리오 type + 생성 계열 + 파생 종류), `difficulty`(선택).
- 문구 규칙(ADR-0026 §4): 질문만 보고 답 종류가 하나로 정해져야 한다. 템플릿마다 2~4가지 표현을 두고 시드로 고른다. 상대 날짜("내일", "다음 주 화요일")는 as_of와 사용자 시간대로 계산해 정답과 맞는지 테스트한다. 제공자 ID를 질문에 넣지 않는다(사람이 부르는 이름·날짜만).
- 정답 계산은 세계에서 다시 한다(truth의 참조가 세계에 있는지, day_agenda 등은 세계 일정으로 재계산해 truth와 일치하는지 단언). 불일치면 생성 실패.
- `forbidden_tools`·`required_tool_calls`는 쓰지 않는다(궤적 규칙은 이번 범위 밖).

## 분할 (ADR-0010 §2)
- 근거 아티팩트(evidence + 정답 계산에 쓴 아티팩트)를 공유하는 과제를 연결 요소로 묶는다. 파생 문항은 원본과 같은 묶음.
- 묶음 단위로 dev/test를 시드로 나누되 목표: test 약 140문항·독립 묶음 100개 이상, dev 약 100문항. 유형별로 test/dev가 고르게(층화). 목표를 못 채우면 생성 실패하고 이유를 출력.
- 정답 지문(정답 내용의 정규화 해시)이 같은 과제는 하나만 남긴다(v0 방식).

## 스모크 (§5)
- dev에서 유형별로 고른 25문항(모든 유형 최소 1, 시드 고정). 목록을 manifest `smoke_task_ids`에 적는다. evalrunner CLI에 `--subset smoke`(manifest 목록만 실행) 옵션 추가. 스모크는 dev 분할만이며 test 봉인 규칙과 충돌하지 않게.

## 표현 변형(LLM)
- `paraphrase.py`: 원본 질문 문장 + 지시(뜻·답 종류·날짜·이름 유지, 한 가지 뜻으로만 읽히게, 존댓말/반말 섞기, 실존 회사·서비스 이름 금지)로 1문장을 받는다. 계열은 그 시나리오의 generator_family 모델(realize_models 설정 재사용, anthropic temperature 생략 규칙 그대로).
- 코드 검증: 원본의 핵심 토큰(이름, 날짜·시각 표기, 금액 등 정답 계산에 쓴 고유 표현) 보존, 길이 상한, real_brand·지시문 검사(realize_check 재사용). 실패 시 최대 3회, 그래도 실패면 그 원본은 건너뛰고 다른 원본으로 대체.
- 비용 안전장치는 realize와 같은 방식(`--max-cost-usd` 필수, 누적 기록, 재개, `--dry-run`). 결과와 비용을 `paraphrase.jsonl`과 manifest에 기록. 응답 원문은 검증 통과 문장만 저장.
- LLM 없이도 나머지 생성이 되도록 `--skip-paraphrase`(파생 24문항 없이 생성) 경로를 둔다. 테스트는 스크립트 LLM.

## 출력 (eval/datasets/orbit-eval/v1/)
- `tasks.jsonl`(Task 스키마), `manifest.json`(name orbit-eval, version v1, generator_git_sha, seed, world·skeleton sha256, 유형별·분할별 수, 묶음 수(dev/test), 중복 제거·버린 수와 사유, smoke_task_ids, 표현 변형 모델·비용, 알려진 한계 notes), `review.jsonl`은 만들지 않는다(③에서).
- 기존 world.json·skeleton.json은 읽기만.
- CLI: `python -m orbit.evaldata.v1.tasks --dataset-dir ../eval/datasets/orbit-eval/v1 --seed <int> [--skip-paraphrase | --max-cost-usd X] [--out <dir>]`(기본 출력은 dataset-dir). 같은 입력·시드·git sha면 tasks.jsonl·manifest가 바이트 단위로 같다(표현 변형은 저장된 paraphrase.jsonl을 재사용해 결정적).

## 완료 조건
- 단위 테스트: 유형별 정답 계산(상대 날짜, LA 시간대, 종일 일정, 취소 제외, mismatch 캘린더 우선, clarify readings가 서로 다름, abstain 대상이 세계에 정말 없음), 쓰기 expected_changes 형식, 분할 묶음 불변식(묶음이 한 분할에만), test 묶음 ≥100, 중복 제거, 스모크 구성, 결정성, 표현 변형 검증·재시도·비용 상한(스크립트 LLM).
- 통합 테스트: 생성된 데이터셋이 dataset.py로 로드되고, 유형마다 한 문항씩 스크립트 LLM의 "정답 제출"로 runflow_v0·assistant_v0 러너를 돌려 성공 판정이 나는지(정답 계산과 채점이 맞물리는지). assistant_v0에서 지원하지 않는 유형(문서·쓰기·리마인더)은 실패로 나오는 것이 정상.
- 실제 세계로 `--skip-paraphrase` 생성을 /private/tmp 아래에 한 번 실행해 manifest 주요 수치(유형별·분할별 수, test 묶음 수)를 보고하고, 표현 변형 `--dry-run` 예상 비용을 보고.
- scripts/check.sh(DB 포함: set -a; source backend/dev.env; set +a) 통과.

## 보고
변경 파일, 유형별 문항 수·분할·묶음 수, 템플릿 예시(유형별 1개), 정답 계산 방식, 명세와 다르게 한 부분, (③ 검수)에서 사람이 봐야 할 점, 멈춘 부분.
