import { runEventsUrl } from "../api/endpoints";
import { isTerminalStatus } from "../api/schemas";
import { parseRunEvent, RUN_EVENT_TYPES, type RunEvent } from "./events";

export interface RunStreamHandlers {
  onEvent: (event: RunEvent) => void;
  /** 알려진 종류인데 형태가 다른 이벤트를 받았다. */
  onInvalidEvent?: () => void;
  /** 연결이 끊겨 브라우저가 재접속을 시도한다(`Last-Event-ID`는 브라우저가 붙인다). */
  onReconnecting?: () => void;
  /** 브라우저가 재접속을 포기했다(401 등). */
  onClosed?: () => void;
}

// EventSource.CLOSED. 생성자 없는 환경(jsdom)에서도 쓸 수 있게 상수로 둔다.
const READY_STATE_CLOSED = 2;

export type EventSourceFactory = (url: string) => EventSource;

const defaultFactory: EventSourceFactory = (url) =>
  new EventSource(url, { withCredentials: false });

/**
 * run 이벤트를 구독하고 구독 해제 함수를 돌려준다.
 * 같은 출처 쿠키로 인증하므로 `withCredentials`는 필요 없다(ADR-0013).
 * 종료 상태 이벤트를 받으면 연결을 닫고, 이미 받은 seq는 무시한다.
 */
export function subscribeRunEvents(
  runId: string,
  handlers: RunStreamHandlers,
  createEventSource: EventSourceFactory = defaultFactory,
): () => void {
  const source = createEventSource(runEventsUrl(runId));
  let lastSeq = 0;
  let closed = false;

  const close = () => {
    if (closed) return;
    closed = true;
    source.close();
  };

  const listener = (message: Event) => {
    if (closed || !(message instanceof MessageEvent)) return;
    const data = typeof message.data === "string" ? message.data : "";
    const parsed = parseRunEvent(message.lastEventId, message.type, data);
    if (parsed.kind === "unknown_type") return;
    if (parsed.kind === "invalid") {
      handlers.onInvalidEvent?.();
      return;
    }
    const { event } = parsed;
    if (event.seq <= lastSeq) return;
    lastSeq = event.seq;
    handlers.onEvent(event);
    if (event.type === "status" && isTerminalStatus(event.payload.status)) close();
  };

  for (const type of RUN_EVENT_TYPES) source.addEventListener(type, listener);

  source.onerror = () => {
    if (closed) return;
    if (source.readyState === READY_STATE_CLOSED) {
      closed = true;
      handlers.onClosed?.();
    } else {
      handlers.onReconnecting?.();
    }
  };

  return close;
}
