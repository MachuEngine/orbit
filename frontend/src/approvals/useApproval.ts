import { useEffect, useState } from "react";
import { getApproval } from "../api/endpoints";
import { isApiError, type ApiErrorKind } from "../api/errors";
import type { Approval } from "../api/schemas";

export type ApprovalLoad =
  | { phase: "loading" }
  | { phase: "ready"; approval: Approval }
  | { phase: "error"; kind: ApiErrorKind };

/** 승인 상세를 한 번 불러온다. `approvalId`가 바뀌면 다시 불러온다. */
export function useApproval(
  approvalId: string,
  fetchApproval: typeof getApproval = getApproval,
): ApprovalLoad {
  const [loaded, setLoaded] = useState<{ id: string; result: ApprovalLoad } | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchApproval(approvalId).then(
      (approval) => {
        if (!cancelled) setLoaded({ id: approvalId, result: { phase: "ready", approval } });
      },
      (error: unknown) => {
        if (cancelled) return;
        const kind = isApiError(error) ? error.kind : "server";
        setLoaded({ id: approvalId, result: { phase: "error", kind } });
      },
    );
    return () => {
      cancelled = true;
    };
  }, [approvalId, fetchApproval]);

  return loaded?.id === approvalId ? loaded.result : { phase: "loading" };
}
