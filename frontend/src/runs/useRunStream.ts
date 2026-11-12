import { useCallback, useEffect, useReducer, useState } from "react";
import {
  initialRunViewState,
  runViewReducer,
  type ChatMessage,
  type RunViewState,
} from "./reducer";
import { subscribeRunEvents, type EventSourceFactory } from "./subscribe";

export type ConnectionState = "idle" | "connected" | "reconnecting" | "closed";

export interface RunStream {
  state: RunViewState;
  connection: ConnectionState;
  addUserMessage: (id: string, text: string) => void;
  /** 화면의 메시지를 바꾸고 run 상태를 비운다. 대화를 바꿀 때 쓴다. */
  replaceMessages: (messages: ChatMessage[]) => void;
}

/** `runId`의 이벤트를 구독해 화면 상태로 모은다. `runId`가 바뀌면 run 상태를 비우고(메시지는 유지) 다시 구독한다. */
export function useRunStream(runId: string | null, factory?: EventSourceFactory): RunStream {
  const [state, dispatch] = useReducer(runViewReducer, initialRunViewState);
  // 연결 상태는 어느 run의 것인지 함께 저장해, run이 바뀌면 자동으로 "connected"로 돌아온다.
  const [reported, setReported] = useState<{
    runId: string;
    value: Exclude<ConnectionState, "idle">;
  } | null>(null);

  useEffect(() => {
    if (runId === null) return;
    dispatch({ type: "run_started" });
    return subscribeRunEvents(
      runId,
      {
        onEvent: (event) => {
          setReported({ runId, value: "connected" });
          dispatch({ type: "event", event });
        },
        onInvalidEvent: () => {
          dispatch({ type: "invalid_event" });
        },
        onReconnecting: () => {
          setReported({ runId, value: "reconnecting" });
        },
        onClosed: () => {
          setReported({ runId, value: "closed" });
        },
      },
      factory,
    );
  }, [runId, factory]);

  const addUserMessage = useCallback((id: string, text: string) => {
    dispatch({ type: "user_message", id, text });
  }, []);

  const replaceMessages = useCallback((messages: ChatMessage[]) => {
    dispatch({ type: "replace_messages", messages });
  }, []);

  const connection: ConnectionState =
    runId === null ? "idle" : reported?.runId === runId ? reported.value : "connected";

  return { state, connection, addUserMessage, replaceMessages };
}
