import { useState } from "react";
import { PRODUCT_NAME } from "../brand";
import { Logo } from "../ui/Logo";
import { loginErrorMessage } from "./loginError";
import type { LoginMethods } from "./useSession";
import "./login.css";

const DEV_LOGIN_FAILED = "로그인하지 못했습니다. 서버가 실행 중인지 확인한 뒤 다시 눌러 주세요.";

/**
 * 로그아웃 상태 화면. Google 로그인은 서버가 로그인을 처리하므로 일반 링크로 이동한다.
 * 개발용 로그인은 서버가 dev 환경이라고 알려 줄 때만 보인다(ADR-0013, ADR-0016).
 * 로고 없는 텍스트 버튼이다: Google 브랜드 가이드의 공식 로고 사용 조건을 확인하지 못했다.
 */
export function LoginScreen({
  methods,
  devLoginFailed,
  loginError,
  onDevSignIn,
}: {
  methods: LoginMethods;
  devLoginFailed: boolean;
  /** Google 로그인 콜백이 돌려준 실패 종류(`/?login_error=`). 없으면 null. */
  loginError: string | null;
  onDevSignIn: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const clickDev = () => {
    setBusy(true);
    void onDevSignIn().finally(() => {
      setBusy(false);
    });
  };
  // 방금 한 개발용 로그인 실패가 있으면 그 문구를, 아니면 Google 로그인 실패 문구를 보인다.
  const message = devLoginFailed
    ? DEV_LOGIN_FAILED
    : loginError !== null
      ? loginErrorMessage(loginError)
      : "";
  return (
    <main className="login">
      <div className="login-card">
        <Logo className="login-logo" />
        <h1 className="visually-hidden">{PRODUCT_NAME} 로그인</h1>
        <p>메일·일정·문서를 대화로 다루려면 로그인해 주세요.</p>
        {methods.google && (
          <a
            className="btn btn-primary"
            href="/api/auth/google/start"
            aria-describedby="login-error"
          >
            Google로 로그인
          </a>
        )}
        {methods.dev && (
          <button
            type="button"
            className="btn btn-secondary"
            disabled={busy}
            aria-describedby="login-error"
            onClick={clickDev}
          >
            {busy ? "로그인하는 중" : "개발용 로그인"}
          </button>
        )}
        {!methods.google && !methods.dev && (
          <p>사용할 수 있는 로그인 방법이 없습니다. 서버 설정을 확인해 주세요.</p>
        )}
        <p id="login-error" className="login-error" role="alert">
          {message}
        </p>
      </div>
    </main>
  );
}

/** 세션 확인에 실패했을 때(서버에 닿지 못함) 보여 주는 화면. */
export function ConnectionErrorScreen() {
  return (
    <main className="login">
      <div className="login-card">
        <Logo className="login-logo" />
        <p role="alert" className="login-error">
          서버에 연결하지 못했습니다. 연결을 확인한 뒤 다시 시도해 주세요.
        </p>
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => {
            window.location.reload();
          }}
        >
          다시 시도
        </button>
      </div>
    </main>
  );
}

export function LoadingScreen() {
  return (
    <main className="login">
      <p role="status">불러오는 중입니다.</p>
    </main>
  );
}
