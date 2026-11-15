import { useCallback, useEffect, useState } from "react";
import {
  disconnectConnection,
  getConnections,
  startDesktopConnect,
  startDocsScope,
  startGoogleConnection,
} from "../api/endpoints";
import type { ConnectionsResponse } from "../api/schemas";
import { getConnectBridge } from "../desktop/bridge";

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

  /**
   * Google 동의 화면으로 갈 주소를 받아 온다. 데스크톱 앱이면 셸이 준비한 PKCE challenge·포트·앱 `state`를
   * 티켓 요청에 묶고, 받은 서버 주소를 호출한 쪽이 셸(`openConnectFlow`)로 시스템 브라우저에서 연다
   * (ADR-0024 §5). 실패하면 예외를 그대로 던진다.
   */
  const start = useCallback(async () => {
    const desktop = getConnectBridge();
    if (desktop === undefined) return startGoogleConnection();
    const prepared = await desktop.prepareConnect({ intent: "connect" });
    return startDesktopConnect({ intent: "connect", ...prepared });
  }, []);

  /** 문서 권한 추가용 주소를 받아 온다(데스크톱이면 티켓 주소). */
  const addDocs = useCallback(async (id: string) => {
    const desktop = getConnectBridge();
    if (desktop === undefined) return startDocsScope(id);
    const prepared = await desktop.prepareConnect({ intent: "docs_scope", connection_id: id });
    return startDesktopConnect({ intent: "docs_scope", connection_id: id, ...prepared });
  }, []);

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
