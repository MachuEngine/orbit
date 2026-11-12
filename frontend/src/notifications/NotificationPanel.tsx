import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { NotificationItem, NotificationSettings } from "../api/schemas";
import { browserPermission, requestBrowserPermission, type BrowserPermission } from "./browser";
import { clockText, formatUpcoming } from "./format";
import { LEAD_OPTIONS, NOTIFY_TEXT as T, kindText } from "./text";
import type { NotificationsApi } from "./useNotifications";
import { useNotificationPanel } from "./useNotificationPanel";
import { formatLastActivity } from "../conversations/format";

export const NOTIFICATION_PANEL_ID = "notification-panel";
const FOCUSABLE = "button:not(:disabled), input:not(:disabled), select:not(:disabled)";

interface Notice {
  tone: "success" | "error";
  text: string;
}

/**
 * 알림 패널(대화 상자). 도착한 알림(읽음 처리), 예정된 알림(취소), 일정 알림 설정, 브라우저 알림 켜기를
 * 한곳에 둔다. 알림 제목은 외부 문자열이므로 모두 텍스트로만 그린다(링크·HTML 해석 없음).
 */
export function NotificationPanel({
  notifications,
  onClose,
  now = Date.now,
}: {
  notifications: NotificationsApi;
  onClose: () => void;
  now?: () => number;
}) {
  const uid = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const upcomingHeadingRef = useRef<HTMLHeadingElement>(null);
  const { upcoming, settings, reloadUpcoming, reloadSettings, cancel, save } =
    useNotificationPanel(true);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [permission, setPermission] = useState<BrowserPermission>(() => browserPermission());
  const current = now();

  // 최신 `onClose`는 ref로 읽는다. 부모가 다시 그릴 때마다 효과가 다시 돌면 포커스가 패널로 튄다.
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });

  // 처음 열릴 때 한 번만 포커스를 옮긴다.
  useEffect(() => {
    panelRef.current?.focus();
  }, []);

  useEffect(() => {
    document.body.classList.add("notify-scroll-lock");
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      // 포커스가 패널 밖으로 나가지 않게 첫·마지막 요소 사이를 순환한다.
      const panel = panelRef.current;
      if (panel === null) return;
      const focusable = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE));
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
      document.body.classList.remove("notify-scroll-lock");
      document.removeEventListener("keydown", onKeyDown);
    };
  }, []);

  const guard = (work: () => Promise<void>, failure: string) => {
    if (busy) return;
    setBusy(true);
    setNotice(null);
    work().then(
      () => {
        setBusy(false);
      },
      () => {
        setBusy(false);
        setNotice({ tone: "error", text: failure });
      },
    );
  };

  const confirmCancel = (id: string) => {
    if (busy) return;
    setBusy(true);
    setNotice(null);
    void cancel(id).then((result) => {
      setBusy(false);
      setConfirming(null);
      upcomingHeadingRef.current?.focus(); // 누른 버튼이 사라졌으므로 제목으로 포커스를 돌린다
      if (result === "failed") setNotice({ tone: "error", text: T.cancelFailed });
      else setNotice({ tone: "success", text: result === "gone" ? T.cancelGone : T.cancelled });
    });
  };

  const changeSettings = (next: NotificationSettings) => {
    if (busy) return;
    setBusy(true);
    setNotice(null);
    void save(next).then((ok) => {
      setBusy(false);
      setNotice(
        ok ? { tone: "success", text: T.settingsSaved } : { tone: "error", text: T.settingsFailed },
      );
    });
  };

  const enableBrowser = () => {
    void requestBrowserPermission().then(setPermission);
  };

  const arrived = notifications.state;

  return createPortal(
    <>
      <button
        type="button"
        className="notify-backdrop"
        tabIndex={-1}
        aria-hidden="true"
        onClick={onClose}
      />
      <div
        id={NOTIFICATION_PANEL_ID}
        ref={panelRef}
        className="notify-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${uid}-title`}
        tabIndex={-1}
      >
        <div className="notify-head">
          <h2 id={`${uid}-title`} className="notify-title">
            {T.panelTitle}
          </h2>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            {T.close}
          </button>
        </div>

        <div role="status" className="notify-notice">
          {notice !== null && notice.tone === "success" && (
            <p className="note tone-success">{notice.text}</p>
          )}
        </div>
        <div role="alert" className="notify-notice">
          {notice !== null && notice.tone === "error" && (
            <p className="note tone-error">{notice.text}</p>
          )}
        </div>

        <section className="notify-section" aria-labelledby={`${uid}-arrived`}>
          <div className="notify-section-head">
            <h3 id={`${uid}-arrived`}>{T.arrivedHeading}</h3>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={busy || arrived.phase !== "ready" || arrived.unread === 0}
              onClick={() => {
                guard(notifications.markAllRead, T.markFailed);
              }}
            >
              {T.markAllRead}
            </button>
          </div>
          {arrived.phase === "loading" && <p className="notify-note">{T.arrivedLoading}</p>}
          {arrived.phase === "error" && (
            <div className="notify-note">
              <p className="tone-error">{T.arrivedError}</p>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => void notifications.refresh()}
              >
                {T.retry}
              </button>
            </div>
          )}
          {arrived.phase === "ready" && arrived.items.length === 0 && (
            <p className="notify-note">{T.arrivedEmpty}</p>
          )}
          {arrived.phase === "ready" && arrived.items.length > 0 && (
            <ul className="notify-list">
              {arrived.items.map((item) => (
                <li key={item.id} className="notify-item" data-read={item.read}>
                  <div className="notify-item-body">
                    <p className="notify-item-title">{item.title}</p>
                    <p className="notify-item-meta">
                      <span>{kindText(item.kind)}</span>
                      <span>{formatLastActivity(item.due_at, current)}</span>
                      {item.event_start !== null && (
                        <span>{T.eventStarts(clockText(item.event_start))}</span>
                      )}
                      {!item.read && <strong className="notify-unread">{T.unreadMark}</strong>}
                    </p>
                  </div>
                  {!item.read && (
                    <button
                      type="button"
                      className="btn btn-secondary"
                      disabled={busy}
                      aria-label={T.markReadLabel(item.title)}
                      onClick={() => {
                        guard(() => notifications.markRead(item.id), T.markFailed);
                      }}
                    >
                      {T.markRead}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="notify-section" aria-labelledby={`${uid}-upcoming`}>
          <h3 id={`${uid}-upcoming`} ref={upcomingHeadingRef} tabIndex={-1}>
            {T.upcomingHeading}
          </h3>
          {upcoming.phase === "loading" && <p className="notify-note">{T.upcomingLoading}</p>}
          {upcoming.phase === "error" && (
            <div className="notify-note">
              <p className="tone-error">{T.upcomingError}</p>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => void reloadUpcoming()}
              >
                {T.retry}
              </button>
            </div>
          )}
          {upcoming.phase === "ready" && upcoming.items.length === 0 && (
            <p className="notify-note">{T.upcomingEmpty}</p>
          )}
          {upcoming.phase === "ready" && upcoming.items.length > 0 && (
            <ul className="notify-list">
              {upcoming.items.map((item) => (
                <UpcomingRow
                  key={item.id}
                  item={item}
                  now={current}
                  busy={busy}
                  confirming={confirming === item.id}
                  onAsk={() => {
                    setConfirming(item.id);
                  }}
                  onKeep={() => {
                    setConfirming(null);
                  }}
                  onConfirm={() => {
                    confirmCancel(item.id);
                  }}
                />
              ))}
            </ul>
          )}
        </section>

        <section className="notify-section" aria-labelledby={`${uid}-settings`}>
          <h3 id={`${uid}-settings`}>{T.settingsHeading}</h3>
          {settings.phase === "loading" && <p className="notify-note">{T.settingsLoading}</p>}
          {settings.phase === "error" && (
            <div className="notify-note">
              <p className="tone-error">{T.settingsError}</p>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => void reloadSettings()}
              >
                {T.retry}
              </button>
            </div>
          )}
          {settings.phase === "ready" && (
            <div className="notify-settings">
              <div className="notify-check">
                <input
                  id={`${uid}-enabled`}
                  type="checkbox"
                  checked={settings.settings.event_reminders_enabled}
                  disabled={busy}
                  aria-describedby={`${uid}-enabled-hint`}
                  onChange={(e) => {
                    changeSettings({
                      ...settings.settings,
                      event_reminders_enabled: e.target.checked,
                    });
                  }}
                />
                <label htmlFor={`${uid}-enabled`}>{T.enabledLabel}</label>
              </div>
              <p id={`${uid}-enabled-hint`} className="notify-note">
                {T.enabledHint}
              </p>
              <div className="notify-field">
                <label htmlFor={`${uid}-lead`}>{T.leadLabel}</label>
                <select
                  id={`${uid}-lead`}
                  value={String(settings.settings.lead_minutes)}
                  disabled={busy || !settings.settings.event_reminders_enabled}
                  onChange={(e) => {
                    changeSettings({ ...settings.settings, lead_minutes: Number(e.target.value) });
                  }}
                >
                  {LEAD_OPTIONS.map((minutes) => (
                    <option key={minutes} value={String(minutes)}>
                      {T.leadOption(minutes)}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          )}
        </section>

        {permission !== "unsupported" && (
          <section className="notify-section" aria-labelledby={`${uid}-browser`}>
            <h3 id={`${uid}-browser`}>{T.browserHeading}</h3>
            {permission === "default" && (
              <>
                <p className="notify-note">{T.browserHint}</p>
                <button type="button" className="btn btn-primary" onClick={enableBrowser}>
                  {T.browserEnable}
                </button>
              </>
            )}
            {permission === "granted" && <p className="notify-note">{T.browserOn}</p>}
            {permission === "denied" && (
              <p className="note tone-warning" role="note">
                {T.browserDenied}
              </p>
            )}
          </section>
        )}
      </div>
    </>,
    document.body,
  );
}

function UpcomingRow({
  item,
  now,
  busy,
  confirming,
  onAsk,
  onKeep,
  onConfirm,
}: {
  item: NotificationItem;
  now: number;
  busy: boolean;
  confirming: boolean;
  onAsk: () => void;
  onKeep: () => void;
  onConfirm: () => void;
}) {
  return (
    <li className="notify-item">
      <div className="notify-item-body">
        <p className="notify-item-title">{item.title}</p>
        <p className="notify-item-meta">
          <span>{kindText(item.kind)}</span>
          <span>{formatUpcoming(item.due_at, now)}</span>
          {item.event_start !== null && <span>{T.eventStarts(clockText(item.event_start))}</span>}
        </p>
        {confirming && (
          <div className="notify-confirm" role="group" aria-label={T.cancelConfirm}>
            <p>{T.cancelConfirm}</p>
            <div className="notify-actions">
              <button type="button" className="btn btn-primary" disabled={busy} onClick={onConfirm}>
                {T.cancelYes}
              </button>
              <button type="button" className="btn btn-secondary" disabled={busy} onClick={onKeep}>
                {T.cancelNo}
              </button>
            </div>
          </div>
        )}
      </div>
      {!confirming && (
        <button
          type="button"
          className="btn btn-secondary"
          disabled={busy}
          aria-label={T.cancelLabel(item.title)}
          onClick={onAsk}
        >
          {T.cancel}
        </button>
      )}
    </li>
  );
}
