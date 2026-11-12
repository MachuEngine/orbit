import { useCallback, useEffect, useState } from "react";
import {
  disconnectConnection,
  getConnections,
  startDocsScope,
  startGoogleConnection,
} from "../api/endpoints";
import type { ConnectionsResponse } from "../api/schemas";

export type ConnectionsState =
  { phase: "loading" } | { phase: "error" } | { phase: "ready"; data: ConnectionsResponse };

/** 연결 목록을 읽고, 연결 시작·해제 요청을 보낸다. 토큰은 이 화면에 오지 않는다. */
export function useConnections() {
  const [state, setState] = useState<ConnectionsState>({ phase: "loading" });

  const reload = useCallback(async () => {
    try {
      setState({ phase: "ready", data: await getConnections() });
    } catch {
      setState({ phase: "error" });
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    getConnections().then(
      (data) => {
        if (!cancelled) setState({ phase: "ready", data });
      },
      () => {
        if (!cancelled) setState({ phase: "error" });
      },
    );
    return () => {
      cancelled = true;
    };
  }, []);

  /** Google 동의 화면 주소를 받아 온다. 실패하면 예외를 그대로 던진다. */
  const start = useCallback(() => startGoogleConnection(), []);

  /** 문서 권한 추가용 Google 동의 화면 주소를 받아 온다. */
  const addDocs = useCallback((id: string) => startDocsScope(id), []);

  const disconnect = useCallback(
    async (id: string) => {
      const result = await disconnectConnection(id);
      await reload();
      return result;
    },
    [reload],
  );

  return { state, reload, start, addDocs, disconnect };
}

/** 채팅 화면 안내용. 실패하면 안내를 보이지 않는다(채팅을 막지 않는다). */
export function useConnectionNotice(): "needs_connection" | null {
  const [notice, setNotice] = useState<"needs_connection" | null>(null);
  useEffect(() => {
    let cancelled = false;
    getConnections().then(
      (data) => {
        if (cancelled) return;
        const active = data.connections.some((c) => c.status === "active");
        setNotice(data.mode === "google" && !active ? "needs_connection" : null);
      },
      () => undefined,
    );
    return () => {
      cancelled = true;
    };
  }, []);
  return notice;
}
