/**
 * 앱 창(서버 출처)에 `window.orbitDesktop`만 노출한다. `ipcRenderer`·Node API는 노출하지 않는다.
 * sandbox preload는 로컬 파일을 require할 수 없어 esbuild가 한 파일로 묶는다.
 */
import { contextBridge, ipcRenderer } from "electron";
import { CHANNELS, VERSION_ARG } from "../shared/channels";

function readVersion(): string {
  const arg = process.argv.find((item) => item.startsWith(VERSION_ARG));
  const value = arg?.slice(VERSION_ARG.length) ?? "";
  return /^[0-9A-Za-z.+-]{1,32}$/.test(value) ? value : "unknown";
}

const orbitDesktop = Object.freeze({
  notify(payload: { id: string; title: string; body?: string }): void {
    // 값을 새 객체로 옮겨 담아 보낸다. 검증은 main이 한다.
    ipcRenderer.send(CHANNELS.notify, {
      id: payload.id,
      title: payload.title,
      body: payload.body ?? "",
    });
  },
  setBadge(count: number): void {
    ipcRenderer.send(CHANNELS.setBadge, count);
  },
  onToggleVoice(callback: () => void): () => void {
    if (typeof callback !== "function") return () => undefined;
    const listener = () => {
      callback();
    };
    ipcRenderer.on(CHANNELS.toggleVoice, listener);
    return () => {
      ipcRenderer.removeListener(CHANNELS.toggleVoice, listener);
    };
  },
  /** 시스템 브라우저에서 Google 로그인을 시작한다. 결과는 `onLoginEvent`로 온다(ADR-0024 §4). */
  startLogin(): void {
    ipcRenderer.send(CHANNELS.startLogin);
  },
  onLoginEvent(callback: (event: { status: string; reason?: string }) => void): () => void {
    if (typeof callback !== "function") return () => undefined;
    const listener = (_event: unknown, raw: unknown) => {
      if (typeof raw !== "object" || raw === null) return;
      const { status, reason } = raw as { status?: unknown; reason?: unknown };
      if (status === "done") callback({ status: "done" });
      else if (status === "failed" && typeof reason === "string")
        callback({ status: "failed", reason });
    };
    ipcRenderer.on(CHANNELS.loginEvent, listener);
    return () => {
      ipcRenderer.removeListener(CHANNELS.loginEvent, listener);
    };
  },
  /**
   * 계정 연결을 시작할 준비를 한다(ADR-0024 §5). main이 PKCE·앱 `state`·loopback 수신기를 만들고
   * `code_challenge`·포트·앱 `state`만 돌려준다(`code_verifier`는 main에만 있다).
   */
  prepareConnect(args: { intent: "connect" | "docs_scope"; connection_id?: string }): Promise<{
    code_challenge: string;
    port: number;
    app_state: string;
  }> {
    return ipcRenderer.invoke(CHANNELS.prepareConnect, {
      intent: args.intent,
      ...(args.connection_id === undefined ? {} : { connection_id: args.connection_id }),
    }) as Promise<{ code_challenge: string; port: number; app_state: string }>;
  },
  /** 연결 완료·실패 이벤트를 받는다. 완료는 앱이 세션으로 서버에 제출한 결과다. */
  onConnectEvent(
    callback: (
      event: { status: "done"; result: string } | { status: "failed"; reason: string },
    ) => void,
  ): () => void {
    if (typeof callback !== "function") return () => undefined;
    const listener = (_event: unknown, raw: unknown) => {
      if (typeof raw !== "object" || raw === null) return;
      const { status, result, reason } = raw as {
        status?: unknown;
        result?: unknown;
        reason?: unknown;
      };
      if (status === "done" && (result === "connected" || result === "docs_added"))
        callback({ status: "done", result });
      else if (status === "failed" && typeof reason === "string")
        callback({ status: "failed", reason });
    };
    ipcRenderer.on(CHANNELS.connectEvent, listener);
    return () => {
      ipcRenderer.removeListener(CHANNELS.connectEvent, listener);
    };
  },
  /** 서버의 계정 연결 시작 주소(`desktop-begin`)만 시스템 브라우저로 연다. main이 다시 검사한다. */
  openConnectFlow(url: string): void {
    ipcRenderer.send(CHANNELS.openConnectFlow, url);
  },
  /** 로그아웃 후 앱 파티션의 쿠키·캐시·저장소를 지운다. */
  clearSession(): void {
    ipcRenderer.send(CHANNELS.clearSession);
  },
  info: Object.freeze({ platform: process.platform, version: readVersion() }),
});

contextBridge.exposeInMainWorld("orbitDesktop", orbitDesktop);
