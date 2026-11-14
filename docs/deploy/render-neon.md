# 데모 배포: Render + Neon

설계 근거는 `docs/adr/ADR-0022-free-tier-demo-deploy.md`다. 이 문서는 그 결정을 실행하는 절차만 적는다. 무료 한도와 가격은 바뀔 수 있으므로 배포 전에 Render·Neon 공식 문서에서 다시 확인한다. 아래 명령의 `<...>` 자리는 직접 채우고, 실제 값은 저장소·이슈·커밋 메시지에 남기지 않는다.

## 구성
- 앱: Render 무료 Web Service 1개(Docker). API, 작업 워커(`ORBIT_EMBEDDED_WORKER=true`), 빌드한 프론트가 한 프로세스에서 돈다.
- DB: Neon 무료 프로젝트(Postgres, pgvector).
- 마이그레이션은 Render 안에서 돌리지 않는다. 로컬에서 `orbit_owner`로 실행한 뒤 배포한다.

## 1. Neon

### 1-1. 역할은 SQL로 만든다
Neon 콘솔이나 API로 만든 역할은 `neon_superuser`의 멤버가 되고, 이 역할에는 `BYPASSRLS`가 있다. 그러면 행 수준 보안(RLS)이 무력화되어 워크스페이스 사이 격리가 깨진다. 그래서 `orbit_owner`와 `orbit_app`은 **Neon 기본 소유자 역할로 접속해서 SQL로** 만든다. 속성은 `backend/db/init/00_roles.sql`과 같다.

```sql
CREATE ROLE orbit_owner LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD '<소유자 비밀번호>';
CREATE ROLE orbit_app   LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD '<앱 비밀번호>';

CREATE EXTENSION IF NOT EXISTS vector;

GRANT CREATE, USAGE ON SCHEMA public TO orbit_owner;
GRANT USAGE ON SCHEMA public TO orbit_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
```

만든 뒤 두 역할이 격리 조건에 맞는지 확인한다. 앱은 시작할 때 같은 조건을 점검하고, 어긋나면 시작을 거부한다.

```sql
SELECT rolname, rolsuper, rolbypassrls,
       (SELECT count(*) FROM pg_auth_members m WHERE m.member = r.oid) AS member_of
  FROM pg_roles r WHERE rolname IN ('orbit_owner', 'orbit_app');
-- 두 행 모두 rolsuper=f, rolbypassrls=f, member_of=0 이어야 한다.
SELECT name, default_version FROM pg_available_extensions WHERE name = 'vector';
-- default_version이 0.8 이상이어야 한다.
```

### 1-2. 직접 연결 주소를 쓴다
Neon은 호스트 이름에 `-pooler`가 붙은 풀러 주소와 붙지 않은 직접 주소를 준다. Orbit은 **직접 주소**만 쓴다. 작업 큐(Procrastinate)가 `LISTEN/NOTIFY`를, 메일 색인 동기화가 세션 advisory lock을 쓰는데, 풀러(PgBouncer 트랜잭션 모드)는 둘 다 지원하지 않기 때문이다.

URL 형식은 `postgresql+psycopg://<역할>:<비밀번호>@<직접 호스트>/<DB 이름>?sslmode=require`다.

### 1-3. 마이그레이션을 로컬에서 실행한다
`orbit_owner` URL로 실행한다. 비밀번호가 셸 기록에 남지 않게 입력 프롬프트로 받는다. `migrations/env.py`가 설정 전체를 읽으므로 `ORBIT_DATABASE_URL`도 필요하다(여기서는 같은 URL을 둔다. 이 셸에서만 쓰고 Render에는 넣지 않는다).

```bash
cd backend
read -s -p "orbit_owner 비밀번호: " OWNER_PW; echo
export ORBIT_MIGRATION_DATABASE_URL="postgresql+psycopg://orbit_owner:${OWNER_PW}@<직접 호스트>/<DB 이름>?sslmode=require"
export ORBIT_DATABASE_URL="$ORBIT_MIGRATION_DATABASE_URL"
uv run alembic upgrade head
unset OWNER_PW ORBIT_MIGRATION_DATABASE_URL ORBIT_DATABASE_URL
```

처음 한 번, 같은 `orbit_owner`로 다음 권한을 준다. 앱이 시작할 때 `alembic_version`을 읽어 DB가 코드의 최신 리비전인지 확인하는데, 이 테이블은 Alembic이 소유자 이름으로 만들어서 `orbit_app`에는 읽기 권한이 없다.

```sql
GRANT SELECT ON alembic_version TO orbit_app;
```

마이그레이션을 추가한 배포에서는 새 이미지를 배포하기 **전에** 같은 방법으로 `alembic upgrade head`를 먼저 실행한다. 순서가 어긋나면 앱이 시작을 거부한다.

## 2. 비밀 키 만들기
배포용 키는 로컬 개발 키를 재사용하지 않고 새로 만든다. 값은 화면에 출력하지 않는다.

```bash
cd backend
uv run python -m orbit.crypto.keygen /절대/경로/prod-data-key        # 데이터 키(KEK), 권한 0600
python3 -c "import secrets; print(secrets.token_urlsafe(48))" > /절대/경로/prod-session-signing-key
chmod 600 /절대/경로/prod-session-signing-key
```

## 3. Render
- New Web Service → 저장소 연결 → Runtime은 **Docker**(저장소 루트의 `Dockerfile`), 플랜은 Free.
- Health Check Path: `/api/healthz`(DB 연결까지 확인한다).
- 시작 명령은 `Dockerfile`의 `CMD`를 그대로 쓴다. 포트는 Render가 주는 `$PORT`다.
- 이미지에 `ORBIT_ENV=prod`와 `ORBIT_STATIC_DIR`가 고정되어 있다. Render 환경 변수 `RENDER`가 있는데 `ORBIT_ENV=dev`이면 앱이 시작을 거부한다.

### Secret Files
Render의 Secret Files에 2번에서 만든 두 파일을 등록한다. 파일은 `/etc/secrets/<파일 이름>`에 놓인다.

| 파일 이름(예) | 내용 | 가리키는 환경 변수 |
| --- | --- | --- |
| `data-key` | 데이터 키(KEK) | `ORBIT_DATA_KEY_FILE=/etc/secrets/data-key` |
| `session-signing-key` | 세션 서명 키 | `ORBIT_SESSION_SIGNING_KEY_FILE=/etc/secrets/session-signing-key` |

prod에서는 데이터 키 파일 권한이 0600보다 넓으면 시작을 거부한다(`ORBIT_DATA_KEY_FILE 권한이 0600보다 넓다`). Render가 Secret Files를 컨테이너에 어떤 권한으로 두는지는 이 저장소에서 확인하지 못했다. 이 오류로 시작하지 못하면 배포 로그의 메시지를 개발자에게 알린다.

### 환경 변수 (값은 Render에서만 입력한다)
| 이름 | 설명 |
| --- | --- |
| `ORBIT_DATABASE_URL` | `orbit_app` 역할의 직접 연결 URL (`postgresql+psycopg://...?sslmode=require`) |
| `ORBIT_EMBEDDED_WORKER` | `true`. API 프로세스 안에서 작업 워커를 함께 돌린다 |
| `ORBIT_CONNECTOR_MODE` | `google`. 내장 워커는 prod에서 이 값이 필요하다 |
| `ORBIT_GOOGLE_CLIENT_ID`, `ORBIT_GOOGLE_CLIENT_SECRET` | Google OAuth 클라이언트 |
| `ORBIT_GOOGLE_REDIRECT_URI` | `https://<배포 도메인>/api/auth/google/callback` |
| `ORBIT_GOOGLE_CONNECT_REDIRECT_URI` | `https://<배포 도메인>/api/connections/google/callback` |
| `ORBIT_LOGIN_ALLOWED_EMAILS` | 로그인 허용 이메일(쉼표 구분). 비어 있으면 prod에서 시작하지 않는다 |
| `ORBIT_DATA_KEY_FILE`, `ORBIT_SESSION_SIGNING_KEY_FILE` | 위 Secret Files 경로 |
| `ORBIT_LLM_PROVIDER`, `ORBIT_LLM_MODEL_ID`, `ORBIT_LLM_REASONING_EFFORT` | LLM 설정. `.env.example`의 LLM 절 참고 |
| `ORBIT_OPENAI_API_KEY` | OpenAI API 키 |
| `ORBIT_EMBEDDING_PROVIDER`, `ORBIT_EMBEDDING_MODEL_ID` | 메일 색인 임베딩(`openai`, `text-embedding-3-small`) |
| `ORBIT_USER_TIMEZONE` | 상대 날짜를 계산할 IANA 시간대(기본 `Asia/Seoul`) |
| `ORBIT_LOG_LEVEL` | 선택. 기본 `INFO` |
| `FORWARDED_ALLOW_IPS` | 선택. 기본 `*`. Render 프록시 주소 대역을 알게 되면 그 값으로 좁힌다 |

LLM 단가(`ORBIT_LLM_PRICE_*`)는 비용 기록이 필요할 때만 넣는다.

### 프록시 헤더
uvicorn은 `--proxy-headers`로 `X-Forwarded-Proto`·`X-Forwarded-For`를 읽는다. Render 서비스는 Render의 프록시를 거쳐서만 닿으므로 기본값을 `*`로 두었다. Render 프록시의 고정 IP 대역은 확인하지 못했다. 이 값은 접근 로그의 클라이언트 주소와 요청 스킴에만 영향을 준다. 쿠키의 `Secure` 속성은 `ORBIT_ENV`로 정해지고 이 헤더에 의존하지 않는다.

## 4. Google Cloud 콘솔
- OAuth 클라이언트의 "승인된 리디렉션 URI"에 두 주소를 추가한다.
  - `https://<배포 도메인>/api/auth/google/callback` (로그인)
  - `https://<배포 도메인>/api/connections/google/callback` (계정 연결)
- OAuth 동의 화면은 "테스트" 상태로 두고, 데모 사용자를 "테스트 사용자"에 등록한다. 같은 이메일을 `ORBIT_LOGIN_ALLOWED_EMAILS`에도 넣는다. 앱 검증 전이라 테스트 사용자만 로그인할 수 있다.

## 5. 배포 후 확인
1. `https://<배포 도메인>/api/healthz`가 `{"status":"ok"}`를 돌려준다.
2. 로그에 `Application startup complete`가 있고, 시작 점검 오류(`StartupRefused`)가 없다.
3. 로그인 콜백 요청이 접근 로그에 쿼리 문자열(`?code=...`) 없이 남는다.
4. 대화를 하나 시작해 작업(run)이 끝까지 처리되는지 본다(내장 워커가 도는지 확인).

## 6. 무료 한도로 생기는 제약
- 15분 동안 요청이 없으면 Render가 서비스를 절전시키고, 깨어나는 데 약 1분이 걸린다. Neon도 5분 비활성이면 정지했다가 다음 연결에서 깨어난다.
- 절전 중에는 메일 색인 주기 동기화와 일정 알림 동기화가 돌지 않는다. 대화 리마인더는 다음 접속 때 목록에 나타나지만, 브라우저 알림은 탭이 열려 있고 서버가 깨어 있을 때만 뜬다.
- 깨어 있게 하는 핑은 쓰지 않는다. 앱이 깨어 있으면 워커 연결 때문에 Neon도 정지하지 않아 월 CU-시간 한도를 넘을 수 있다.
- 메모리는 512MB다. ADR-0022는 최대 RSS가 450MB를 넘으면 이 구성을 다시 검토하도록 정했다. 2026-11-14 로컬 Docker(arm64) 측정에서 내장 워커 모드 프로세스는 시작 직후 약 135MiB였고, Kiwi 형태소 분석기를 처음 불러온 뒤 최대 약 640MiB였다(합성 데이터, 외부 호출 없음). 이 값은 450MB 기준을 넘으므로 배포 전에 Kiwi 로딩 방식을 포함해 구성을 다시 정해야 한다.
- Neon 저장 한도는 1GB다. 데모 계정은 메일 색인 범위를 30일로 안내한다.
- 워커가 예외로 끝나면 프로세스가 종료되고 Render가 다시 시작한다.
