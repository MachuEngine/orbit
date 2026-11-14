import { useEffect, useRef, useState } from "react";
import type { ConversationListState } from "./useConversationList";
import { conversationTitle, formatLastActivity } from "./format";
import { Icon } from "../ui/Icon";
import "./conversations.css";

export const CONVERSATION_PANEL_ID = "conversation-panel";

/**
 * 대화 목록. 넓은 화면에서는 왼쪽에 고정하고(`wide`), 좁은 화면에서는 열었을 때만 그려서
 * 대화 상자(`drawer`)로 보여 준다. 대화 상자는 열리면 포커스를 받고 Esc나 배경을 누르면 닫히며,
 * 열려 있는 동안 배경 스크롤을 막는다.
 */
export function ConversationPanel({
  layout,
  list,
  currentId,
  busy,
  loadError,
  onNew,
  onSelect,
  onRetry,
  onClose,
  now,
}: {
  layout: "wide" | "drawer";
  list: ConversationListState;
  currentId: string | undefined;
  /** 메시지를 보내는 중이거나 대화를 불러오는 중이라 다른 대화로 옮길 수 없다. */
  busy: boolean;
  /** 대화를 불러오지 못했을 때의 안내 문구 */
  loadError: string | null;
  onNew: () => void;
  onSelect: (id: string) => void;
  onRetry: () => void;
  onClose: () => void;
  now?: number;
}) {
  const drawer = layout === "drawer";
  const panelRef = useRef<HTMLDivElement>(null);
  // 상대 시각("3분 전")이 오래된 채로 남지 않게 1분마다 기준 시각을 갱신한다.
  const [clock, setClock] = useState(() => now ?? Date.now());
  useEffect(() => {
    if (now !== undefined) return;
    const timer = setInterval(() => {
      setClock(Date.now());
    }, 60_000);
    return () => {
      clearInterval(timer);
    };
  }, [now]);

  useEffect(() => {
    if (!drawer) return;
    panelRef.current?.focus();
    document.body.classList.add("conversation-scroll-lock");
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      // 포커스가 패널 밖으로 나가지 않게 첫·마지막 요소 사이를 순환한다.
      const panel = panelRef.current;
      if (panel === null) return;
      const focusable = Array.from(panel.querySelectorAll<HTMLElement>("button:not(:disabled)"));
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (first === undefined || last === undefined) {
        event.preventDefault();
        panel.focus();
        return;
      }
      const active = document.activeElement;
      if (event.shiftKey && (active === first || active === panel || !panel.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !panel.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.classList.remove("conversation-scroll-lock");
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [drawer, onClose]);

  const content = (
    <>
      <div className="conversation-head">
        <h2 className="conversation-heading">대화</h2>
        {drawer && (
          <button type="button" className="btn btn-ink" onClick={onClose}>
            닫기
          </button>
        )}
      </div>
      <button type="button" className="btn btn-ink" disabled={busy} onClick={onNew}>
        새 대화
      </button>
      {loadError !== null && (
        <p className="conversation-note tone-error" role="alert">
          {loadError}
        </p>
      )}
      {list.status === "loading" && (
        <p className="conversation-note conversation-loading">
          불러오는 중입니다.
          <span className="loading-dots" aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
        </p>
      )}
      {list.status === "error" && (
        <div className="conversation-note">
          <p className="note tone-error">
            <Icon name="error" />
            대화 목록을 불러오지 못했습니다.
          </p>
          <button type="button" className="btn btn-secondary" onClick={onRetry}>
            다시 불러오기
          </button>
        </div>
      )}
      {list.status === "ready" && list.items.length === 0 && (
        <p className="conversation-note rule-left">아직 대화가 없습니다. 첫 요청을 보내 보세요.</p>
      )}
      {list.items.length > 0 && (
        <ul className="conversation-list">
          {list.items.map((item) => {
            const current = item.id === currentId;
            const time = formatLastActivity(item.last_activity_at, now ?? clock);
            return (
              <li key={item.id}>
                <button
                  type="button"
                  className="conversation-item"
                  aria-current={current ? "true" : undefined}
                  disabled={busy}
                  onClick={() => {
                    onSelect(item.id);
                  }}
                >
                  <span className="conversation-item-title">{conversationTitle(item.title)}</span>
                  {time !== "" && <span className="conversation-item-time">{time}</span>}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );

  if (!drawer) {
    return (
      <nav id={CONVERSATION_PANEL_ID} className="conversation-panel" aria-label="대화 목록">
        {content}
      </nav>
    );
  }
  return (
    <>
      <button
        type="button"
        className="conversation-backdrop"
        tabIndex={-1}
        aria-hidden="true"
        onClick={onClose}
      />
      <div className="conversation-drawer-frame">
        <div
          id={CONVERSATION_PANEL_ID}
          ref={panelRef}
          className="conversation-panel conversation-drawer"
          role="dialog"
          aria-modal="true"
          aria-label="대화 목록"
          tabIndex={-1}
        >
          {content}
        </div>
      </div>
    </>
  );
}
