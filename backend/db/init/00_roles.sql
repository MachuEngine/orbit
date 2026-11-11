-- Orbit DB 역할 생성 (ADR-0004 규칙 4). 개발용 compose와 CI가 같은 파일을 쓴다.
-- 슈퍼유저(postgres)로 대상 DB에 접속해 psql로 실행한다. 여러 번 실행해도 된다.
--
-- 비밀번호는 환경 변수에서 읽는다(psql \getenv, PostgreSQL 15 이상 클라이언트).
--   ORBIT_OWNER_PASSWORD, ORBIT_APP_PASSWORD
-- 없으면 로컬 개발 전용 값을 쓴다. 운영에서는 반드시 환경 변수를 준다.
--
--   orbit_owner: 스키마·테이블 소유자. 마이그레이션 전용.
--   orbit_app:   앱 접속 역할. 소유자가 아니고 NOBYPASSRLS.

\set ON_ERROR_STOP on

\getenv owner_pw ORBIT_OWNER_PASSWORD
\if :{?owner_pw}
\else
  \set owner_pw orbit_dev_owner_only
\endif
\getenv app_pw ORBIT_APP_PASSWORD
\if :{?app_pw}
\else
  \set app_pw orbit_dev_app_only
\endif

SELECT 'CREATE ROLE orbit_owner LOGIN'
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'orbit_owner') \gexec
SELECT 'CREATE ROLE orbit_app LOGIN'
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'orbit_app') \gexec

ALTER ROLE orbit_owner LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD :'owner_pw';
ALTER ROLE orbit_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD :'app_pw';

-- 마이그레이션이 public 스키마에 객체를 만든다. 확장은 슈퍼유저가 미리 만들어 둔다.
GRANT CREATE, USAGE ON SCHEMA public TO orbit_owner;
GRANT USAGE ON SCHEMA public TO orbit_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE EXTENSION IF NOT EXISTS vector;
