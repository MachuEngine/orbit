import { useCallback, useEffect, useState } from "react";
import { devLogin, getSession, logout } from "../api/endpoints";
import { isApiError } from "../api/errors";

export type SessionState =
  { phase: "loading" } | { phase: "signed_out" } | { phase: "signed_in" } | { phase: "error" };

/** 세션 확인과 임시 로그인·로그아웃. 401이면 로그인이 필요한 상태로 본다. */
export function useSession() {
  const [state, setState] = useState<SessionState>({ phase: "loading" });

  useEffect(() => {
    let cancelled = false;
    getSession().then(
      () => {
        if (!cancelled) setState({ phase: "signed_in" });
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
      setState({ phase: "error" });
    }
  }, []);

  const signOut = useCallback(async () => {
    try {
      await logout();
    } finally {
      setState({ phase: "signed_out" });
    }
  }, []);

  return { state, signIn, signOut };
}
