import { useCallback, useEffect, useRef, useState } from "react";
import {
  addIndexScope,
  deleteIndexScope,
  getIndexStatus,
  getMailLabels,
  syncIndexNow,
} from "../api/endpoints";
import { isApiError } from "../api/errors";
import type { IndexStatus, MailLabel } from "../api/schemas";

export const DEFAULT_POLL_MS = 5000;
/** 폴링이 이만큼 연속으로 실패하면 멈추고 사용자가 다시 불러오게 한다. */
export const MAX_POLL_FAILURES = 3;

/** 폴링을 멈춘 이유. 로그인이 만료됐거나(401) 연속으로 실패했다. */
export type PollHalt = "unauthorized" | "failures" | null;

export type IndexState =
  { phase: "loading" } | { phase: "error" } | { phase: "ready"; status: IndexStatus };

export type LabelsState =
  | { phase: "loading" }
  | { phase: "error"; detail: string | null }
  | { phase: "ready"; labels: MailLabel[] };

/** 동기화가 대기하거나 진행 중일 때만 다시 읽는다(서버가 `sync_queued`로 알려 준다). */
export function shouldPoll(state: IndexState): boolean {
  return state.phase === "ready" && state.status.sync_queued;
}

/**
 * 연결 하나의 색인 범위·상태·라벨을 읽고, 범위 추가·삭제와 "지금 동기화"를 보낸다.
 * 동기화가 끝나지 않았으면 `pollMs`마다 상태를 다시 읽고, 끝나거나 화면을 떠나면 멈춘다.
 */
export function useMailIndex(connectionId: string, pollMs: number = DEFAULT_POLL_MS) {
  const [index, setIndex] = useState<IndexState>({ phase: "loading" });
  const [labels, setLabels] = useState<LabelsState>({ phase: "loading" });
  const alive = useRef(true);
  const inFlight = useRef(false);
  const failures = useRef(0);
  const [halt, setHalt] = useState<PollHalt>(null);

  const loadStatus = useCallback(async () => {
    try {
      const status = await getIndexStatus(connectionId);
      if (alive.current) setIndex({ phase: "ready", status });
    } catch {
      // 이미 읽은 상태가 있으면 유지하고(폴링 한 번의 실패로 화면을 비우지 않는다), 없을 때만 오류다.
      if (alive.current) setIndex((prev) => (prev.phase === "ready" ? prev : { phase: "error" }));
    }
  }, [connectionId]);

  const loadLabels = useCallback(async () => {
    try {
      const found = await getMailLabels(connectionId);
      if (alive.current) setLabels({ phase: "ready", labels: found });
    } catch (error) {
      if (alive.current) {
        setLabels({ phase: "error", detail: isApiError(error) ? error.detail : null });
      }
    }
  }, [connectionId]);

  const reloadLabels = useCallback(async () => {
    setLabels({ phase: "loading" });
    await loadLabels();
  }, [loadLabels]);

  // 처음 읽기는 효과 안에서 상태를 바로 바꾸지 않도록 응답 콜백에서만 바꾼다.
  useEffect(() => {
    alive.current = true;
    getIndexStatus(connectionId).then(
      (status) => {
        if (alive.current) setIndex({ phase: "ready", status });
      },
      () => {
        if (alive.current) setIndex({ phase: "error" });
      },
    );
    getMailLabels(connectionId).then(
      (found) => {
        if (alive.current) setLabels({ phase: "ready", labels: found });
      },
      (error: unknown) => {
        if (alive.current) {
          setLabels({ phase: "error", detail: isApiError(error) ? error.detail : null });
        }
      },
    );
    return () => {
      alive.current = false;
    };
  }, [connectionId]);

  /** 폴링 한 번. 앞선 요청이 끝나지 않았으면 건너뛰고, 연속 실패를 센다. */
  const pollOnce = useCallback(() => {
    if (inFlight.current) return;
    inFlight.current = true;
    getIndexStatus(connectionId)
      .then(
        (status) => {
          failures.current = 0;
          if (alive.current) setIndex({ phase: "ready", status });
        },
        (error: unknown) => {
          failures.current += 1;
          if (!alive.current) return;
          if (isApiError(error) && error.kind === "unauthorized") setHalt("unauthorized");
          else if (failures.current >= MAX_POLL_FAILURES) setHalt("failures");
        },
      )
      .finally(() => {
        inFlight.current = false;
      });
  }, [connectionId]);

  /** 멈춘 폴링을 풀고 상태를 한 번 읽는다. 동기화가 아직 진행 중이면 폴링이 다시 시작된다. */
  const resume = useCallback(async () => {
    failures.current = 0;
    setHalt(null);
    await loadStatus();
  }, [loadStatus]);

  const polling = shouldPoll(index) && halt === null;
  useEffect(() => {
    if (!polling) return undefined;
    const timer = window.setInterval(pollOnce, pollMs);
    return () => {
      window.clearInterval(timer);
    };
  }, [polling, pollMs, pollOnce]);

  const addScope = useCallback(
    async (labelId: string, windowDays: number) => {
      await addIndexScope(connectionId, { label_id: labelId, window_days: windowDays });
      await loadStatus();
    },
    [connectionId, loadStatus],
  );

  const removeScope = useCallback(
    async (scopeId: string) => {
      await deleteIndexScope(connectionId, scopeId);
      await loadStatus();
    },
    [connectionId, loadStatus],
  );

  /** 새로 등록했으면 true, 이미 대기 중이어서 등록하지 않았으면 false. */
  const syncNow = useCallback(async () => {
    const queued = await syncIndexNow(connectionId);
    await loadStatus();
    return queued;
  }, [connectionId, loadStatus]);

  return {
    index,
    labels,
    reload: loadStatus,
    reloadLabels,
    halt,
    resume,
    addScope,
    removeScope,
    syncNow,
  };
}
