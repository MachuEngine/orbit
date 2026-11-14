import { useEffect, useRef } from "react";
import type { Approval } from "../api/schemas";
import { Icon } from "../ui/Icon";
import {
  describeApproval,
  hasProvenanceWarning,
  personReason,
  provenanceTag,
  type DetailRow,
  type Person,
} from "./describe";
import {
  actionLabel,
  approvalStatusText,
  formatRemaining,
  isIrreversible,
  systemClock,
  type Clock,
} from "./logic";
import { useApproval } from "./useApproval";
import { useApprovalDecision, type DecisionState } from "./useApprovalDecision";
import { useRemainingMs } from "./useCountdown";
import "./approval.css";

/** 승인 상세를 보여 주고 승인 의사만 서버에 보낸다. 실행 여부는 서버가 판단한다. */
export function ApprovalPanel({
  approvalId,
  clock = systemClock,
}: {
  approvalId: string;
  clock?: Clock;
}) {
  const load = useApproval(approvalId);
  if (load.phase === "loading") {
    return (
      <p className="note tone-info" role="status">
        <Icon name="clock" />
        <span>승인 내용을 불러오고 있습니다.</span>
      </p>
    );
  }
  if (load.phase === "error") {
    return (
      <p className="note tone-error" role="alert">
        <Icon name="error" />
        <span>승인 내용을 불러오지 못했습니다. 새로고침한 뒤 다시 확인해 주세요.</span>
      </p>
    );
  }
  return <ApprovalDetail approval={load.approval} clock={clock} />;
}

function decisionMessage(state: DecisionState): { tone: "info" | "error"; text: string } | null {
  switch (state.phase) {
    case "idle":
      return null;
    case "submitting":
      return { tone: "info", text: "결정을 전달하고 있습니다." };
    case "done":
      return {
        tone: "info",
        text:
          state.decision === "approve"
            ? "결정을 전달했습니다. 서버가 실행한 뒤 결과를 알려 드립니다."
            : "결정을 전달했습니다. 실행하지 않습니다.",
      };
    case "conflict":
      return {
        tone: "error",
        text:
          state.reason === "expired"
            ? "승인 시간이 지나 실행하지 않았습니다."
            : "이미 결정된 요청입니다.",
      };
    case "error":
      switch (state.kind) {
        case "unauthorized":
          return {
            tone: "error",
            text: "로그인이 만료되었습니다. 다시 로그인한 뒤 확인해 주세요.",
          };
        case "network":
          return {
            tone: "error",
            text: "결정이 전달됐는지 확인하지 못했습니다. 다시 눌러도 두 번 실행되지 않습니다.",
          };
        default:
          return { tone: "error", text: "결정을 전달하지 못했습니다. 다시 눌러 주세요." };
      }
  }
}

function ApprovalDetail({ approval, clock }: { approval: Approval; clock: Clock }) {
  const { state, submit } = useApprovalDecision(approval.id);
  const remaining = useRemainingMs(approval.expires_at, clock);
  const label = actionLabel(approval.tool_name);
  const view = describeApproval(approval);
  const warned = hasProvenanceWarning(view);
  const decided = approval.status !== "pending";
  const locked =
    decided ||
    state.phase === "submitting" ||
    state.phase === "done" ||
    state.phase === "conflict" ||
    remaining === 0;
  const message = decisionMessage(state);
  const root = useRef<HTMLElement>(null);

  // 카드가 나타나면 키보드 사용자가 바로 내용을 읽고 버튼으로 갈 수 있게 카드로 포커스를 옮긴다.
  // 버튼이 아니라 카드 자체에 두므로 Enter를 눌러도 실행되지 않는다.
  useEffect(() => {
    root.current?.focus();
    root.current?.scrollIntoView({ block: "start" });
  }, []);

  return (
    <section
      className="approval retro retro-hard"
      aria-labelledby="approval-title"
      ref={root}
      tabIndex={-1}
    >
      <header className="approval-header">
        <h2 id="approval-title">{label?.name ?? "실행 전 확인"}</h2>
      </header>
      {isIrreversible(approval.tool_name) && (
        <p className="approval-irreversible">
          <Icon name="ban" />
          <span>되돌릴 수 없음</span>
        </p>
      )}
      <p className="approval-lead">
        {isIrreversible(approval.tool_name)
          ? "내용을 확인해 주세요. 보낸 메일은 회수할 수 없습니다."
          : "실행하기 전에 내용을 확인해 주세요."}
      </p>
      {warned && (
        <p className="approval-alert" role="alert">
          <Icon name="warning" />
          <span>확인이 필요한 값이 들어 있습니다. 표시된 항목을 확인해 주세요.</span>
        </p>
      )}
      {view.hiddenKeys.length > 0 && (
        <p className="approval-alert" role="alert">
          <Icon name="warning" />
          <span>
            표시되지 않은 항목이 있습니다({view.hiddenKeys.join(", ")}). 내용을 모두 확인할 수
            없으면 실행하지 않는 것이 안전합니다.
          </span>
        </p>
      )}
      <dl className="approval-details">
        {view.rows.map((row) => (
          <Row key={row.label} row={row} />
        ))}
        {view.strayWarnings.length > 0 && (
          <Row row={{ kind: "people", label: "확인이 필요한 주소", people: view.strayWarnings }} />
        )}
      </dl>
      <p className="approval-expiry">
        <Icon name="clock" />
        <span>
          {remaining === 0 ? "만료되었습니다." : `남은 시간 ${formatRemaining(remaining)}`}
        </span>
      </p>
      <div className="approval-actions">
        <button
          type="button"
          className="btn btn-primary"
          disabled={locked}
          onClick={() => void submit("approve")}
        >
          {label?.confirm ?? "실행"}
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          disabled={locked}
          onClick={() => void submit("reject")}
        >
          실행하지 않기
        </button>
      </div>
      <div className="approval-states">
        <div role="status">
          {decided && <p>{approvalStatusText(approval.status)}</p>}
          {message?.tone === "info" && <p>{message.text}</p>}
        </div>
        <div role="alert">{message?.tone === "error" && <p>{message.text}</p>}</div>
      </div>
    </section>
  );
}

function Row({ row }: { row: DetailRow }) {
  return (
    <div className="approval-row">
      <dt>{row.label}</dt>
      <dd>
        {row.kind === "text" && <span className="num">{row.value}</span>}
        {row.kind === "people" && (
          <ul className="people">
            {row.people.map((p) => (
              <PersonItem key={p.email} person={p} />
            ))}
          </ul>
        )}
        {row.kind === "longtext" &&
          (row.collapsible ? (
            <>
              <p className="longtext-excerpt">{row.excerpt}</p>
              <details>
                <summary>전체 {row.label} 보기</summary>
                <p className="longtext">{row.text}</p>
              </details>
            </>
          ) : (
            <p className="longtext">{row.text}</p>
          ))}
      </dd>
    </div>
  );
}

function personBadge(person: Person): string {
  if (person.source === null) return "출처 확인 안 됨";
  return person.source === "model" ? "비서가 만든 값" : "외부 출처";
}

function PersonItem({ person }: { person: Person }) {
  const reason = personReason(person);
  const tag = provenanceTag(person.source);
  return (
    <li className={person.flagged ? "person person-flagged" : "person"}>
      {person.flagged && (
        <span className="badge badge-warning">
          <Icon name="warning" />
          <span>{personBadge(person)}</span>
        </span>
      )}{" "}
      <span className="person-email">{person.email}</span>
      {tag !== null && <span className="person-tag"> ({tag})</span>}
      {reason !== null && <span className="person-reason">{reason}</span>}
    </li>
  );
}
