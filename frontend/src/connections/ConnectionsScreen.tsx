import { useEffect, useRef, useState } from "react";
import type { Connection } from "../api/schemas";
import { PRODUCT_NAME } from "../brand";
import { Icon } from "../ui/Icon";
import { Logo } from "../ui/Logo";
import {
  CONNECTED_MESSAGE,
  REVOKE_MESSAGES,
  connectionErrorMessage,
  scopeLabels,
  type ConnectionResult,
} from "./text";
import { useConnections } from "./useConnections";
import "./connections.css";

interface Notice {
  tone: "success" | "warning" | "error";
  text: string;
}

function initialNotice(result: ConnectionResult | null): Notice | null {
  if (result === null) return null;
  return result.kind === "connected"
    ? { tone: "success", text: CONNECTED_MESSAGE }
    : { tone: "error", text: connectionErrorMessage(result.error) };
}

/**
 * 설정 > 계정 연결. 연결 상태, 계정 이메일, 허용한 권한을 보여 주고 연결·해제를 시작한다.
 * 연결은 Google 동의 화면으로 이동해서 끝낸다. 해제는 확인 단계를 한 번 거친다.
 * `navigate`는 Google 동의 화면으로 이동하는 함수이며 테스트에서 바꿔 끼운다.
 */
export function ConnectionsScreen({
  onBack,
  result,
  navigate = (url) => {
    window.location.assign(url);
  },
}: {
  onBack: () => void;
  /** Google 연결을 마치고 돌아온 결과. 없으면 null. */
  result: ConnectionResult | null;
  navigate?: (url: string) => void;
}) {
  const { state, start, disconnect } = useConnections();
  const [notice, setNotice] = useState<Notice | null>(() => initialNotice(result));
  const [confirming, setConfirming] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const headingRef = useRef<HTMLHeadingElement>(null);

  const connect = () => {
    setBusy(true);
    setNotice(null);
    start().then(
      (url) => {
        navigate(url);
      },
      () => {
        setBusy(false);
        setNotice({
          tone: "error",
          text: "연결을 시작하지 못했습니다. 잠시 뒤 다시 눌러 주세요.",
        });
      },
    );
  };

  const confirmDisconnect = (id: string) => {
    setBusy(true);
    disconnect(id).then(
      (res) => {
        setBusy(false);
        setConfirming(null);
        // 눌렀던 버튼이 사라졌으므로 제목으로 포커스를 돌려 화면낭독기 사용자가 위치를 잃지 않게 한다.
        headingRef.current?.focus();
        setNotice({
          tone: res.provider_revoke === "failed" ? "warning" : "success",
          text: REVOKE_MESSAGES[res.provider_revoke],
        });
      },
      () => {
        setBusy(false);
        setNotice({
          tone: "error",
          text: "연결을 해제하지 못했습니다. 잠시 뒤 다시 눌러 주세요.",
        });
      },
    );
  };

  return (
    <main className="conn">
      <header className="conn-header">
        <Logo className="conn-logo" />
        <button type="button" className="btn btn-secondary" onClick={onBack}>
          채팅으로 돌아가기
        </button>
      </header>

      <div className="conn-body">
        <h1 ref={headingRef} tabIndex={-1}>
          계정 연결
        </h1>
        {/* 알림 영역은 비어 있어도 남겨 두어야 내용이 생길 때 화면낭독기가 읽는다. */}
        <div role="status" className="conn-notice">
          {notice !== null && notice.tone !== "error" && <NoticeLine notice={notice} />}
        </div>
        <div role="alert" className="conn-notice">
          {notice !== null && notice.tone === "error" && <NoticeLine notice={notice} />}
        </div>

        {state.phase === "loading" && <p role="status">불러오는 중입니다.</p>}
        {state.phase === "error" && (
          <p role="alert" className="tone-error">
            연결 정보를 불러오지 못했습니다. 잠시 뒤 다시 열어 주세요.
          </p>
        )}
        {state.phase === "ready" && (
          <>
            {state.data.mode === "sim" && (
              <p className="note tone-info">
                <Icon name="info" />
                <span>
                  지금은 개발용 모의 데이터로 동작합니다. Google 계정을 연결하지 않아도 됩니다.
                </span>
              </p>
            )}
            {state.data.connections.length === 0 ? (
              <section className="conn-card" aria-labelledby="conn-none">
                <h2 id="conn-none">Google 계정</h2>
                <p>
                  연결된 계정이 없습니다. 연결하면 {PRODUCT_NAME}이 메일과 일정을 대신 살펴볼 수
                  있습니다.
                </p>
                <p className="conn-hint">
                  메일 보내기와 일정 변경은 매번 내가 확인한 뒤에만 실행됩니다.
                </p>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={busy || !state.data.connect_available}
                  onClick={connect}
                >
                  Google 계정 연결
                </button>
                {!state.data.connect_available && (
                  <p className="conn-hint">
                    서버에 Google 연결 설정이 없어 지금은 연결할 수 없습니다.
                  </p>
                )}
              </section>
            ) : (
              state.data.connections.map((item) => (
                <ConnectionCard
                  key={item.id}
                  item={item}
                  busy={busy}
                  canConnect={state.data.connect_available}
                  confirming={confirming === item.id}
                  onAskDisconnect={() => {
                    setConfirming(item.id);
                  }}
                  onCancel={() => {
                    setConfirming(null);
                  }}
                  onConfirm={() => {
                    confirmDisconnect(item.id);
                  }}
                  onReconnect={connect}
                />
              ))
            )}
          </>
        )}
      </div>
    </main>
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

function ConnectionCard({
  item,
  busy,
  canConnect,
  confirming,
  onAskDisconnect,
  onCancel,
  onConfirm,
  onReconnect,
}: {
  item: Connection;
  busy: boolean;
  canConnect: boolean;
  confirming: boolean;
  onAskDisconnect: () => void;
  onCancel: () => void;
  onConfirm: () => void;
  onReconnect: () => void;
}) {
  const active = item.status === "active";
  const labels = scopeLabels(item.scopes);
  const confirmRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const wasConfirming = useRef(false);
  // 확인 단계가 열리면 그 안내로, 취소해서 닫히면 연 버튼으로 포커스를 옮긴다.
  useEffect(() => {
    if (confirming) confirmRef.current?.focus();
    else if (wasConfirming.current) triggerRef.current?.focus();
    wasConfirming.current = confirming;
  }, [confirming]);
  return (
    <section className="conn-card" aria-labelledby={`conn-${item.id}`}>
      <h2 id={`conn-${item.id}`}>Google 계정</h2>
      <p className={`note ${active ? "tone-success" : "tone-error"}`}>
        <Icon name={active ? "success" : "error"} />
        <span>{active ? "연결됨" : "연결에 문제가 있습니다. 다시 연결해 주세요."}</span>
      </p>
      <p>
        <span className="conn-label">계정</span> {item.account_email}
      </p>
      {active && labels.length > 0 && (
        <div>
          <p className="conn-label">허용한 권한</p>
          <ul className="conn-scopes">
            {labels.map((label) => (
              <li key={label}>{label}</li>
            ))}
          </ul>
        </div>
      )}
      {confirming ? (
        <div
          className="conn-confirm"
          role="group"
          aria-label="연결 해제 확인"
          ref={confirmRef}
          tabIndex={-1}
        >
          <p>
            연결을 해제하면 {PRODUCT_NAME}이 이 Google 계정의 메일과 일정을 더 이상 읽거나 바꿀 수
            없습니다. 해제할까요?
          </p>
          <div className="conn-actions">
            <button type="button" className="btn btn-primary" disabled={busy} onClick={onConfirm}>
              연결 해제
            </button>
            <button type="button" className="btn btn-secondary" disabled={busy} onClick={onCancel}>
              취소
            </button>
          </div>
        </div>
      ) : (
        <div className="conn-actions">
          {!active && (
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy || !canConnect}
              onClick={onReconnect}
            >
              Google 계정 다시 연결
            </button>
          )}
          <button
            type="button"
            className="btn btn-secondary"
            disabled={busy}
            onClick={onAskDisconnect}
            ref={triggerRef}
          >
            연결 해제
          </button>
        </div>
      )}
      {!active && !canConnect && !confirming && (
        <p className="conn-hint">서버에 Google 연결 설정이 없어 지금은 다시 연결할 수 없습니다.</p>
      )}
    </section>
  );
}
