import { useCallback, useEffect, useRef, useState } from "react";
import { listConversations } from "../api/endpoints";
import type { ConversationSummary } from "../api/schemas";

export type ConversationListState =
  | { status: "loading"; items: ConversationSummary[] }
  | { status: "ready"; items: ConversationSummary[] }
  | { status: "error"; items: ConversationSummary[] };

/**
 * 대화 목록을 불러온다. `refresh`로 다시 불러오며, 이미 받은 목록이 있으면 다시 불러오는 도중이나
 * 실패했을 때도 그 목록을 지우지 않는다(실패는 처음 불러올 때만 알린다).
 */
export function useConversationList() {
  const [state, setState] = useState<ConversationListState>({ status: "loading", items: [] });
  const latest = useRef(0);
  const mounted = useRef(true);
  const hasItems = useRef(false);

  const refresh = useCallback(() => {
    const ticket = ++latest.current;
    listConversations().then(
      (response) => {
        if (!mounted.current || ticket !== latest.current) return;
        hasItems.current = true;
        setState({ status: "ready", items: response.conversations });
      },
      () => {
        if (!mounted.current || ticket !== latest.current) return;
        if (hasItems.current) return;
        setState({ status: "error", items: [] });
      },
    );
  }, []);

  /** 목록을 처음 불러오는 상태(또는 실패 뒤 재시도)로 되돌리고 다시 불러온다. */
  const retry = useCallback(() => {
    hasItems.current = false;
    setState({ status: "loading", items: [] });
    refresh();
  }, [refresh]);

  useEffect(() => {
    mounted.current = true;
    refresh();
    return () => {
      mounted.current = false;
    };
  }, [refresh]);

  return { state, refresh, retry };
}
