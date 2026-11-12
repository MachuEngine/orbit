# ADR-0020 — 문서 연결 (Google Drive 읽기)

- 상태: 채택(구조). 문서 쓰기와 문서 색인은 이번 범위가 아니다(아래 "보류").
- 작성일: 2026-11-12
- 근거: 세부 설계 D1-1(커넥터 경계)·D6-1(외부 콘텐츠)·D7-1(색인 범위)·D8-1(핸들, 비공개 `private-docs/`), ADR-0008(커넥터 계약), ADR-0011(읽기 비서), ADR-0016(연결·점진 권한), ADR-0017(대화 맥락), ADR-0018(데이터 봉투)

## 문제
비서가 메일·일정만 다룬다. "지난주 회의록 찾아서 요약해줘"처럼 Drive 문서를 찾거나 읽는 질문에 답하지 못한다.

## 결정

### 1. 권한
- `https://www.googleapis.com/auth/drive.readonly`(제한 범위)를 받는다. Drive 전체를 이름·내용으로 검색하려면 이 범위가 필요하다. `gmail.readonly` 때문에 출시 전 제한 범위 보안 평가는 어차피 받아야 하고, 이 범위는 평가 대상에 더해진다.
- 문서 권한은 선택 권한이다. 기존 연결은 그대로 `active`이고, 연결 화면의 "문서 권한 추가"로 점진 동의(`include_granted_scopes=true`)를 받는다. 처음 연결할 때도 메일·캘린더 범위만 필수다.
- 권한 추가 콜백은 같은 계정(`sub`)의 활성 연결이면 그 행의 `scopes`와 토큰 암호문을 새 값으로 바꾼다. 새 연결을 만들지 않으므로 메일 색인(ADR-0019)이 유지된다. 이전 refresh token은 철회하지 않는다(같은 클라이언트의 권한 전체가 철회될 수 있다, ADR-0016). 다른 계정이면 거절한다.
- Google Cloud 프로젝트에서 Drive API를 켜고 동의 화면에 범위를 추가해야 한다(개발자 작업).

### 2. 커넥터 계약 `DriveConnector` (ADR-0008 4절 뒤에 추가 문단)
| 메서드 | 내용 |
| --- | --- |
| `search_files(query: FileQuery, *, max_results, page_token) -> FilePage` | 구조화된 조건: 텍스트(이름 또는 본문 포함), 종류(문서·스프레드시트·프레젠테이션·PDF·기타), 수정일 이후·이전. 휴지통 제외, 최근 수정순 |
| `get_file(file_id) -> DriveFile` | 이름, 종류, MIME 유형, 수정 시각, 소유자(이름·주소), 크기 |
| `read_file_text(file_id, *, max_chars) -> FileText` | 텍스트와 잘림 여부. Docs는 `text/plain`, Sheets는 첫 시트 `text/csv`, Slides는 `text/plain`로 내보내고, 일반 텍스트 파일은 원문을 읽는다. 그 밖의 형식은 `UnsupportedFile` |

- 모델이 Drive 검색 문법(`q`)을 직접 쓰지 않는다. 어댑터가 구조화된 조건을 `q`로 바꾸고 문자열 값의 따옴표·역슬래시를 이스케이프한다. Sim은 같은 의미로 구현한다.
- 링크(`webViewLink` 등)는 계약에 넣지 않는다(D6-1). 첨부·이미지·외부 링크를 읽어 오지 않는다.
- 범위가 없으면(403 `insufficientPermissions`) 새 사유 `scope_missing`, 형식이 지원되지 않거나 내보내기 한도(10MB)를 넘으면 `UnsupportedFile`.
- Sim Provider와 Google Adapter가 같은 계약 테스트를 통과한다. Sim 세계 스냅샷에 계정별 `files`를 선택 필드로 추가한다. 기존 평가 스냅샷은 바뀌지 않는다.

### 3. 비서 도구 (실사용 `runflow`만)
- `search_docs(text, kind, modified_after, modified_before, max_results)`: 결과 파일마다 작업 집합에 `D` 핸들(D1, D2…)을 등록하고 이름·종류·수정 시각·소유자를 돌려준다.
- `read_doc(handle, max_chars)`: 핸들로만 파일을 가리킨다. 본문은 길이 상한(기존 `limit_external`)으로 자르고 잘림을 표시한다.
- 결과는 데이터 봉투(ADR-0018)로 감싸고, 본문은 출처 분류에서 외부 콘텐츠다. 소유자 주소는 메일 `from`·일정 참석자와 같이 `workspace_data`로 분류한다(공유받은 파일의 소유자도 메일 발신자처럼 제3자일 수 있으나, 쓰기는 지금처럼 승인으로 막는다). 작업 집합의 대화 범위 유지(ADR-0017)와 `[참조 가능한 대상]`(합성 `conversation_context`)에 문서 핸들(이름·수정 시각)을 포함한다.
- 권한이 없으면 도구는 `scope_missing`을 돌려주고 run 이벤트 `connector_notice`(service=`drive`)로 화면에 "문서 권한을 추가하세요"를 띄운다. 프롬프트 `runflow-v0.6`.
- 평가 경로(`assistant_v0`, `evalrunner`)에는 추가하지 않는다. v0 회귀에 영향이 없다.

### 4. 저장
문서 원문·이름은 저장하지 않는다. 도구 결과로 `run_message`(RLS)에 남는 것은 기존 메일 결과와 같다. 그래프 상태와 작업 인자에는 ID만 둔다(ADR-0006).

## 보류
- 문서 쓰기(생성·수정·공유): 승인 경로(ADR-0012)를 그대로 쓸 수 있지만 범위(`drive.file` 등)와 위험을 따로 정한다.
- 문서 색인(RAG): ADR-0019 테이블에 `kind=document`로 넣는 방식. 사용자가 고른 폴더만(D7-1). 읽기 도구를 써 본 뒤 정한다.
- 공유 드라이브, PDF·이미지 텍스트 추출.

## 검토한 대안
| 대안 | 채택하지 않은 이유 |
| --- | --- |
| `drive.file` + Google Picker | 고른 파일만 읽어 범위가 작지만 Drive 검색을 못 하고, 화면에 Google 외부 스크립트와 API 키가 필요하다 |
| Docs API(`documents.get`) | 구조가 풍부하지만 Docs만 다루고 범위가 하나 더 필요하다. 텍스트 내보내기로 충분하다 |
| 모델이 Drive `q` 문법을 직접 작성 | 문법 오류·주입 위험이 있고 Sim과 동작이 갈라진다 |
| 문서 권한을 필수로 | 기존 연결이 모두 재동의해야 하고, 문서를 안 쓰는 사용자에게 제한 범위를 강요한다 |
