// 개발 전용 화면 미리보기의 가짜 백엔드. `window.fetch`와 `EventSource`를 이 파일의 구현으로 바꾼다.
// 실제 네트워크 요청은 나가지 않는다. 모르는 요청은 404로 돌려준다.
import { PREVIEW_REPLY, type FakeRunEvent, type SceneData } from "./scenes";

interface Reply {
  status: number;
  /** null이면 본문이 없는 응답(204) */
  body: unknown;
}

const ok = (body: unknown): Reply => ({ status: 200, body });
const NOT_FOUND: Reply = { status: 404, body: { detail: "preview_not_found" } };
const UNSUPPORTED: Reply = { status: 404, body: { detail: "preview_unsupported" } };

/** 요청 한 건을 처리하는 가짜 서버. 읽음 처리·알림 취소·설정 저장은 이 인스턴스 안에서만 반영된다. */
export class PreviewBackend {
  private readonly data: SceneData;
  private newRuns = 0;

  constructor(data: SceneData) {
    this.data = structuredClone(data);
  }

  /** 응답 본문과 상태를 계산한다. `path`는 쿼리를 포함할 수 있다. */
  handle(method: string, path: string, body: unknown): Reply {
    const route = path.split("?")[0] ?? path;
    const m = (pattern: RegExp) => pattern.exec(route);
    const d = this.data;

    if (method === "GET") {
      if (route === "/api/session") {
        if (d.sessionError === true)
          return { status: 500, body: { detail: "preview_session_error" } };
        return ok(d.session);
      }
      if (route === "/api/connections") return ok(d.connections);
      const index = m(/^\/api\/connections\/([^/]+)\/index$/);
      if (index !== null) {
        if (d.indexError === true) return { status: 500, body: { detail: "preview_index_error" } };
        const found = d.indexes[decodeURIComponent(index[1] ?? "")];
        return found === undefined ? NOT_FOUND : ok(found);
      }
      if (m(/^\/api\/connections\/([^/]+)\/labels$/) !== null) return ok({ labels: d.labels });
      if (route === "/api/conversations") {
        if (d.listMode === "error") return { status: 500, body: { detail: "preview_list_error" } };
        return ok({ conversations: d.conversations });
      }
      const conversation = m(/^\/api\/conversations\/([^/]+)$/);
      if (conversation !== null) {
        const found = d.details[decodeURIComponent(conversation[1] ?? "")];
        return found === undefined ? NOT_FOUND : ok(found);
      }
      const approval = m(/^\/api\/approvals\/([^/]+)$/);
      if (approval !== null) {
        const found = d.approvals[decodeURIComponent(approval[1] ?? "")];
        return found === undefined ? NOT_FOUND : ok(found);
      }
      if (route === "/api/notifications") {
        return ok({
          notifications: d.notifications,
          unread_count: d.notifications.filter((item) => !item.read).length,
        });
      }
      if (route === "/api/notifications/upcoming") return ok({ notifications: d.upcoming });
      if (route === "/api/notification-settings") return ok(d.notificationSettings);
      return UNSUPPORTED;
    }

    if (method === "POST") {
      if (route === "/api/logout") return { status: 204, body: null };
      if (route === "/api/dev/login") {
        if (d.devLoginFails === true)
          return { status: 500, body: { detail: "preview_login_error" } };
        return { status: 204, body: null };
      }
      if (route === "/api/runs") {
        this.newRuns += 1;
        return ok({
          run_id: `run-new-${String(this.newRuns)}`,
          conversation_id: `conv-new-${String(this.newRuns)}`,
          status: "queued",
        });
      }
      const decision = m(/^\/api\/approvals\/([^/]+)\/decision$/);
      if (decision !== null) {
        const approved =
          typeof body === "object" && body !== null && "decision" in body
            ? body.decision === "approve"
            : false;
        return ok({ status: approved ? "approved" : "rejected" });
      }
      if (route === "/api/notifications/read-all") {
        const unread = d.notifications.filter((item) => !item.read);
        for (const item of unread) item.read = true;
        return ok({ updated: unread.length });
      }
      const read = m(/^\/api\/notifications\/([^/]+)\/read$/);
      if (read !== null) {
        const id = decodeURIComponent(read[1] ?? "");
        const item = d.notifications.find((n) => n.id === id);
        if (item === undefined) return NOT_FOUND;
        item.read = true;
        return ok({ id });
      }
      return UNSUPPORTED;
    }

    if (method === "PUT" && route === "/api/notification-settings") {
      const saved = body as SceneData["notificationSettings"];
      d.notificationSettings = saved;
      return ok(saved);
    }

    if (method === "DELETE") {
      const cancel = m(/^\/api\/notifications\/([^/]+)$/);
      if (cancel !== null) {
        const id = decodeURIComponent(cancel[1] ?? "");
        const before = d.upcoming.length;
        d.upcoming = d.upcoming.filter((item) => item.id !== id);
        return d.upcoming.length === before
          ? { status: 409, body: { detail: "gone" } }
          : ok({ id });
      }
    }
    return UNSUPPORTED;
  }

  /** run의 실시간 이벤트. 이 장면이 모르는 새 run은 미리보기 응답 한 줄로 끝난다. */
  eventsFor(runId: string): FakeRunEvent[] {
    const known = this.data.runEvents[runId];
    if (known !== undefined) return known;
    return [
      { type: "status", payload: { status: "running" } },
      { type: "assistant_text", payload: { step: 1, text: PREVIEW_REPLY } },
      { type: "status", payload: { status: "succeeded" } },
    ];
  }

  /** `window.fetch` 대신 쓰는 함수. 네트워크를 쓰지 않는다. */
  readonly fetch: typeof fetch = (input, init) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const method = (init?.method ?? "GET").toUpperCase();
    let body: unknown;
    if (typeof init?.body === "string") {
      try {
        body = JSON.parse(init.body);
      } catch {
        body = undefined;
      }
    }
    // 목록 응답이 오지 않는 장면: 요청을 끝내지 않아 "불러오는 중" 상태가 유지된다.
    if (
      this.data.listMode === "pending" &&
      method === "GET" &&
      /^\/api\/conversations(\?|$)/.test(url)
    ) {
      return new Promise<Response>(() => undefined);
    }
    const reply = this.handle(method, url, body);
    return Promise.resolve(
      reply.body === null
        ? new Response(null, { status: reply.status })
        : new Response(JSON.stringify(reply.body), {
            status: reply.status,
            headers: { "Content-Type": "application/json" },
          }),
    );
  };

  /** `EventSource` 대신 쓰는 생성자. 구독이 시작되면 그 run의 이벤트를 차례로 흘려 보낸다. */
  readonly eventSourceClass: typeof EventSource = createEventSourceClass((runId) =>
    this.eventsFor(runId),
  );
}

function createEventSourceClass(eventsFor: (runId: string) => FakeRunEvent[]): typeof EventSource {
  class PreviewEventSource extends EventTarget {
    static readonly CONNECTING = 0;
    static readonly OPEN = 1;
    static readonly CLOSED = 2;
    readyState = 1;
    onerror: ((event: Event) => void) | null = null;
    readonly withCredentials = false;

    constructor(readonly url: string) {
      super();
      const runId = decodeURIComponent(/\/api\/runs\/([^/]+)\/events/.exec(url)?.[1] ?? "");
      // 구독자가 리스너를 붙인 뒤에 보내야 하므로 한 박자 늦춘다.
      setTimeout(() => {
        eventsFor(runId).forEach((event, index) => {
          if (this.readyState === 2) return;
          this.dispatchEvent(
            new MessageEvent(event.type, {
              data: JSON.stringify(event.payload),
              lastEventId: String(index + 1),
            }),
          );
        });
      }, 0);
    }

    close() {
      this.readyState = 2;
    }
  }
  return PreviewEventSource as unknown as typeof EventSource;
}

/** 이 창의 `fetch`와 `EventSource`를 가짜로 바꾼다. 미리보기 진입점에서만 부른다. */
export function installPreviewBackend(backend: PreviewBackend): void {
  window.fetch = backend.fetch;
  Object.defineProperty(window, "EventSource", {
    value: backend.eventSourceClass,
    configurable: true,
    writable: true,
  });
}
