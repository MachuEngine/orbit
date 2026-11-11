import type { Approval } from "../api/schemas";
import {
  actionLabel,
  formatRemaining,
  isIrreversible,
  provenanceWarnings,
  systemClock,
  type Clock,
} from "./logic";
import { useApproval } from "./useApproval";
import { useApprovalDecision } from "./useApprovalDecision";
import { useRemainingMs } from "./useCountdown";

/** 승인 상세를 보여 주고 승인 의사만 서버에 보낸다. 실행 여부는 서버가 판단한다. */
export function ApprovalPanel({
  approvalId,
  clock = systemClock,
}: {
  approvalId: string;
  clock?: Clock;
}) {
  const load = useApproval(approvalId);
  if (load.phase === "loading") return <p role="status">승인 내용을 불러오고 있습니다.</p>;
  if (load.phase === "error") return <p role="alert">승인 내용을 불러오지 못했습니다.</p>;
  return <ApprovalDetail approval={load.approval} clock={clock} />;
}

function ApprovalDetail({ approval, clock }: { approval: Approval; clock: Clock }) {
  const { state, submit } = useApprovalDecision(approval.id);
  const remaining = useRemainingMs(approval.expires_at, clock);
  const label = actionLabel(approval.tool_name);
  const warnings = provenanceWarnings(approval.provenance);
  const locked = state.phase === "submitting" || state.phase === "done" || remaining === 0;

  return (
    <section aria-labelledby="approval-title">
      <h2 id="approval-title">{label?.name ?? "실행 전 확인"}</h2>
      <p>실행하기 전에 내용을 확인해 주세요.</p>
      {isIrreversible(approval.tool_name) && <p>보낸 뒤에는 되돌릴 수 없습니다.</p>}
      <details open>
        <summary>인자</summary>
        <pre>{JSON.stringify(approval.args, null, 2)}</pre>
      </details>
      {warnings.length > 0 && (
        <div role="alert">
          <p>메일·문서 본문이나 모델이 만든 값이 들어 있습니다. 직접 말한 값인지 확인해 주세요.</p>
          <ul>
            {warnings.map((warning) => (
              <li key={warning.email}>{warning.email}</li>
            ))}
          </ul>
        </div>
      )}
      <p>{remaining === 0 ? "만료되었습니다." : `만료까지 ${formatRemaining(remaining)}`}</p>
      <button type="button" disabled={locked} onClick={() => void submit("approve")}>
        {label?.confirm ?? "실행"}
      </button>
      <button type="button" disabled={locked} onClick={() => void submit("reject")}>
        취소
      </button>
      {state.phase === "done" && <p role="status">결정을 전달했습니다.</p>}
      {state.phase === "conflict" && (
        <p role="alert">
          {state.reason === "expired"
            ? "승인 시간이 지나 실행하지 않았습니다."
            : "이미 결정된 요청입니다."}
        </p>
      )}
      {state.phase === "error" && (
        <p role="alert">결정을 전달하지 못했습니다. 다시 시도해 주세요.</p>
      )}
    </section>
  );
}
