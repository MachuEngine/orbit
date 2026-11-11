import { useCallback, useRef, useState } from "react";
import { decideApproval } from "../api/endpoints";
import { isApiError } from "../api/errors";
import type { Decision } from "../api/schemas";

export type DecisionState =
  | { phase: "idle" }
  | { phase: "submitting" }
  | { phase: "done"; decision: Decision }
  | { phase: "conflict"; reason: "expired" | "already_decided" }
  | { phase: "error"; kind: "unauthorized" | "csrf" | "network" | "other" };

export type DecideFn = typeof decideApproval;

/**
 * 승인 결정을 한 번만 보낸다. 전송 중이거나 끝난 뒤의 추가 호출은 무시한다.
 * 409는 이미 결정됐거나 만료된 것이라 재시도하지 않고 상태로만 알린다.
 * 화면은 의사만 보내고 실행 결과는 run 이벤트로 받는다.
 */
export function useApprovalDecision(approvalId: string, decide: DecideFn = decideApproval) {
  const [state, setState] = useState<DecisionState>({ phase: "idle" });
  const inFlight = useRef(false);
  const settled = useRef(false);

  const submit = useCallback(
    async (decision: Decision) => {
      if (inFlight.current || settled.current) return;
      inFlight.current = true;
      setState({ phase: "submitting" });
      try {
        await decide(approvalId, decision);
        settled.current = true;
        setState({ phase: "done", decision });
      } catch (error) {
        if (isApiError(error) && error.kind === "conflict") {
          settled.current = true;
          setState({
            phase: "conflict",
            reason: error.detail === "expired" ? "expired" : "already_decided",
          });
        } else if (isApiError(error) && ["unauthorized", "csrf", "network"].includes(error.kind)) {
          setState({ phase: "error", kind: error.kind as "unauthorized" | "csrf" | "network" });
        } else {
          setState({ phase: "error", kind: "other" });
        }
      } finally {
        inFlight.current = false;
      }
    },
    [approvalId, decide],
  );

  return { state, submit };
}
