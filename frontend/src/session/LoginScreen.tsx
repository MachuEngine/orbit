import { useState } from "react";
import { PRODUCT_NAME } from "../brand";
import { Logo } from "../ui/Logo";
import "./login.css";

/** 로그아웃 상태 화면. 개발 환경에서만 열리는 임시 로그인이다(ADR-0013). */
export function LoginScreen({
  failed,
  onSignIn,
}: {
  failed: boolean;
  onSignIn: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const click = () => {
    setBusy(true);
    void onSignIn().finally(() => {
      setBusy(false);
    });
  };
  return (
    <main className="login">
      <div className="login-card">
        <Logo className="login-logo" />
        <h1 className="visually-hidden">{PRODUCT_NAME} 로그인</h1>
        <p>메일·일정·문서를 대화로 다루려면 로그인해 주세요.</p>
        <button
          type="button"
          className="btn btn-primary"
          disabled={busy}
          aria-describedby="login-error"
          onClick={click}
        >
          {busy ? "로그인하는 중" : "개발용 로그인"}
        </button>
        <p id="login-error" className="login-error" role="alert">
          {failed && "로그인하지 못했습니다. 서버가 실행 중인지 확인한 뒤 다시 눌러 주세요."}
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
