import { useEffect, useId, useRef, useState } from "react";
import { isApiError } from "../api/errors";
import type { IndexScope, IndexStatus } from "../api/schemas";
import { Icon } from "../ui/Icon";
import {
  INDEX_TEXT as T,
  WINDOW_OPTIONS,
  agoText,
  isSelectableLabel,
  labelDisplayName,
  labelsErrorMessage,
  scopeDisplayName,
  syncErrorMessage,
  windowLabel,
} from "./text";
import { DEFAULT_POLL_MS, useMailIndex } from "./useMailIndex";

interface Notice {
  tone: "success" | "warning" | "error";
  text: string;
}

/**
 * 연결 화면의 "메일 검색 색인" 영역(ADR-0019). 라벨·기간 범위를 고르고, 색인 상태를 보고, 지금 동기화한다.
 * 라벨 이름은 외부 문자열이므로 텍스트로만 렌더링한다. `now`와 `pollMs`는 테스트에서 바꿔 끼운다.
 */
export function MailIndexSection({
  connectionId,
  pollMs = DEFAULT_POLL_MS,
  now = () => new Date(),
}: {
  connectionId: string;
  pollMs?: number;
  now?: () => Date;
}) {
  const { index, labels, reload, reloadLabels, addScope, removeScope, syncNow, halt, resume } =
    useMailIndex(connectionId, pollMs);
  const [labelId, setLabelId] = useState("");
  const [days, setDays] = useState(WINDOW_OPTIONS[1]?.days ?? 90);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const uid = useId();

  const submit = (event: React.SyntheticEvent) => {
    event.preventDefault();
    if (labelId === "" || busy) return;
    setBusy(true);
    setNotice(null);
    addScope(labelId, days).then(
      () => {
        setBusy(false);
        setLabelId("");
        setNotice({ tone: "success", text: T.added });
      },
      (error: unknown) => {
        setBusy(false);
        const unknown = isApiError(error) && error.detail === "unknown_label";
        setNotice({ tone: "error", text: unknown ? T.unknownLabel : T.addFailed });
        if (unknown) void reloadLabels();
      },
    );
  };

  const confirmDelete = (scopeId: string) => {
    setBusy(true);
    setNotice(null);
    removeScope(scopeId).then(
      () => {
        setBusy(false);
        setConfirming(null);
        headingRef.current?.focus(); // 누른 버튼이 사라졌으므로 제목으로 포커스를 돌린다
        setNotice({ tone: "success", text: T.deleted });
      },
      () => {
        setBusy(false);
        setNotice({ tone: "error", text: T.deleteFailed });
      },
    );
  };

  const sync = () => {
    setBusy(true);
    setNotice(null);
    syncNow().then(
      (queued) => {
        setBusy(false);
        setNotice({ tone: "success", text: queued ? T.syncRequested : T.syncAlready });
      },
      () => {
        setBusy(false);
        setNotice({ tone: "error", text: T.syncFailed });
      },
    );
  };

  return (
    <div className="idx" aria-labelledby={`${uid}-h`} role="group">
      <h3 id={`${uid}-h`} ref={headingRef} tabIndex={-1}>
        {T.heading}
      </h3>
      <p className="conn-hint">{T.intro}</p>
      <div role="status" className="idx-notice">
        {notice !== null && notice.tone !== "error" && <NoticeLine notice={notice} />}
      </div>
      <div role="alert" className="idx-notice">
        {notice !== null && notice.tone === "error" && <NoticeLine notice={notice} />}
      </div>

      {index.phase === "loading" && <p role="status">{T.loading}</p>}
      {index.phase === "error" && (
        <div className="idx-block">
          <p role="alert" className="tone-error">
            {T.loadFailed}
          </p>
          <button type="button" className="btn btn-secondary" onClick={() => void reload()}>
            {T.retry}
          </button>
        </div>
      )}
      {index.phase === "ready" && (
        <>
          <form className="idx-form" onSubmit={submit}>
            <div className="idx-field">
              <label htmlFor={`${uid}-label`}>{T.labelField}</label>
              <select
                id={`${uid}-label`}
                value={labelId}
                disabled={busy || labels.phase !== "ready"}
                onChange={(e) => {
                  setLabelId(e.target.value);
                }}
              >
                <option value="">{T.chooseLabel}</option>
                {labels.phase === "ready" &&
                  labels.labels.filter(isSelectableLabel).map((label) => (
                    <option key={label.id} value={label.id}>
                      {labelDisplayName(label)}
                    </option>
                  ))}
              </select>
            </div>
            <div className="idx-field">
              <label htmlFor={`${uid}-days`}>{T.periodField}</label>
              <select
                id={`${uid}-days`}
                value={String(days)}
                disabled={busy}
                onChange={(e) => {
                  setDays(Number(e.target.value));
                }}
              >
                {WINDOW_OPTIONS.map((o) => (
                  <option key={o.days} value={String(o.days)}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
            <button type="submit" className="btn btn-primary" disabled={busy || labelId === ""}>
              {busy ? T.adding : T.add}
            </button>
          </form>
          {labels.phase === "loading" && <p className="conn-hint">{T.loading}</p>}
          {labels.phase === "error" && (
            <div className="idx-block">
              <p role="alert" className="note tone-error">
                <Icon name="error" />
                <span>{labelsErrorMessage(labels.detail)}</span>
              </p>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => void reloadLabels()}
              >
                {T.retry}
              </button>
            </div>
          )}

          {index.status.scopes.length === 0 ? (
            <p className="conn-hint">{T.noScopes}</p>
          ) : (
            <ul className="idx-scopes" aria-label={T.scopeListLabel}>
              {index.status.scopes.map((scope) => (
                <ScopeItem
                  key={scope.id}
                  scope={scope}
                  busy={busy}
                  confirming={confirming === scope.id}
                  onAsk={() => {
                    setConfirming(scope.id);
                  }}
                  onCancel={() => {
                    setConfirming(null);
                  }}
                  onConfirm={() => {
                    confirmDelete(scope.id);
                  }}
                />
              ))}
            </ul>
          )}

          {halt !== null && (
            <div className="idx-block">
              <p role="alert" className="note tone-error">
                <Icon name="error" />
                <span>{halt === "unauthorized" ? T.pollUnauthorized : T.pollStopped}</span>
              </p>
              {halt === "failures" && (
                <button type="button" className="btn btn-secondary" onClick={() => void resume()}>
                  {T.retry}
                </button>
              )}
            </div>
          )}
          <StatusBlock status={index.status} now={now} busy={busy} onSync={sync} />
        </>
      )}
    </div>
  );
}

function NoticeLine({ notice }: { notice: Notice }) {
  return (
    <p className={`note tone-${notice.tone}`}>
      <Icon name={notice.tone} />
      <span>{notice.text}</span>
    </p>
  );
}

function ScopeItem({
  scope,
  busy,
  confirming,
  onAsk,
  onCancel,
  onConfirm,
}: {
  scope: IndexScope;
  busy: boolean;
  confirming: boolean;
  onAsk: () => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const name = scopeDisplayName(scope);
  const confirmRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const wasConfirming = useRef(false);
  useEffect(() => {
    if (confirming) confirmRef.current?.focus();
    else if (wasConfirming.current) triggerRef.current?.focus();
    wasConfirming.current = confirming;
  }, [confirming]);
  return (
    <li className="idx-scope">
      <div className="idx-scope-row">
        <span className="idx-scope-name">{name}</span>
        <span className="conn-label">{windowLabel(scope.window_days)}</span>
        <span className={`idx-state ${scope.synced ? "tone-success" : "tone-warning"}`}>
          <Icon name={scope.synced ? "success" : "clock"} />
          {scope.synced ? T.scopeDone : T.scopeWaiting}
        </span>
        {!confirming && (
          <button
            type="button"
            className="btn btn-secondary"
            disabled={busy}
            aria-label={`${name} ${T.deleteAction}`}
            onClick={onAsk}
            ref={triggerRef}
          >
            {T.deleteAction}
          </button>
        )}
      </div>
      {confirming && (
        <div
          className="conn-confirm"
          role="group"
          aria-label={T.deleteConfirmLabel}
          ref={confirmRef}
          tabIndex={-1}
        >
          <p>{T.deleteConfirm}</p>
          <div className="conn-actions">
            <button type="button" className="btn btn-primary" disabled={busy} onClick={onConfirm}>
              {T.deleteAction}
            </button>
            <button type="button" className="btn btn-secondary" disabled={busy} onClick={onCancel}>
              {T.cancel}
            </button>
          </div>
        </div>
      )}
    </li>
  );
}

function StatusBlock({
  status,
  now,
  busy,
  onSync,
}: {
  status: IndexStatus;
  now: () => Date;
  busy: boolean;
  onSync: () => void;
}) {
  const hasScopes = status.scopes.length > 0;
  const last = status.last_synced_at;
  return (
    <div className="idx-status">
      <dl className="idx-facts">
        <div>
          <dt className="conn-label">{T.countLabel}</dt>
          <dd className="num">{status.indexed_mail_count.toLocaleString("ko-KR")}통</dd>
        </div>
        <div>
          <dt className="conn-label">{T.lastSyncLabel}</dt>
          <dd>
            {last === null ? (
              T.neverSynced
            ) : (
              <time dateTime={last} title={new Date(last).toLocaleString("ko-KR")}>
                {agoText(last, now())}
              </time>
            )}
          </dd>
        </div>
      </dl>
      <div role="status">
        {status.sync_queued ? (
          <p className="note tone-info">
            <Icon name="clock" />
            <span>{T.syncing}</span>
          </p>
        ) : status.status === "pending" && hasScopes ? (
          <p className="note tone-info">
            <Icon name="clock" />
            <span>{T.waitingForSync}</span>
          </p>
        ) : null}
      </div>
      {status.status === "error" && hasScopes && (
        <p className="note tone-error" role="alert">
          <Icon name="error" />
          <span>
            {syncErrorMessage(status.error_kind)} {last !== null && T.keepsOldIndex}
          </span>
        </p>
      )}
      <button
        type="button"
        className="btn btn-secondary"
        disabled={busy || status.sync_queued || !hasScopes}
        onClick={onSync}
      >
        {T.syncNow}
      </button>
    </div>
  );
}
