import { useCallback, useEffect, useState } from "react";
import { devLogin, getSession, logout } from "../api/endpoints";
import { isApiError } from "../api/errors";
import { clearLoginErrorFromUrl, readLoginError } from "./loginError";

/** 서버가 열어 둔 로그인 수단. 개발용 로그인은 dev 서버에서만 켜진다. */
export interface LoginMethods {
  dev: boolean;
  google: boolean;
}

/** 서버가 수단을 알려 주기 전에는 가장 기본인 Google 로그인만 보인다. */
const DEFAULT_METHODS: LoginMethods = { dev: false, google: true };

export type SessionState =
  | { phase: "loading" }
  /** `loginFailed`는 방금 한 개발용 로그인 시도가 실패했다는 뜻이다. */
  | { phase: "signed_out"; loginFailed?: boolean }
  | { phase: "signed_in" }
  | { phase: "error" };

/**
 * 세션 확인과 개발용 로그인·로그아웃. 401이면 로그인이 필요한 상태로 본다.
 * `methods`는 서버가 `/api/session`으로 알려 준 로그인 수단이다.
 * `loginError`는 Google 로그인 콜백이 주소에 남긴 실패 종류다(처음 한 번만 읽고 주소에서 지운다).
 */
export function useSession() {
  const [state, setState] = useState<SessionState>({ phase: "loading" });
  const [methods, setMethods] = useState<LoginMethods>(DEFAULT_METHODS);
  const [loginError] = useState(() => readLoginError(window.location.search));

  useEffect(() => {
    clearLoginErrorFromUrl();
  }, []);

  useEffect(() => {
    let cancelled = false;
    getSession().then(
      (session) => {
        if (cancelled) return;
        setMethods({ dev: session.dev_login, google: session.google_login });
        setState({ phase: session.authenticated ? "signed_in" : "signed_out" });
      },
      (error: unknown) => {
        if (cancelled) return;
        setState(
          isApiError(error) && error.kind === "unauthorized"
            ? { phase: "signed_out" }
            : { phase: "error" },
        );
      },
    );
    return () => {
      cancelled = true;
    };
  }, []);

  const signIn = useCallback(async () => {
    try {
      await devLogin();
      setState({ phase: "signed_in" });
    } catch {
      // 세션 확인은 됐으므로 서버 연결 오류 화면이 아니라 로그인 화면에서 실패를 알린다.
      setState({ phase: "signed_out", loginFailed: true });
    }
  }, []);

  const signOut = useCallback(async () => {
    try {
      await logout();
    } finally {
      setState({ phase: "signed_out" });
    }
  }, []);

  return { state, methods, signIn, signOut, loginError };
}
