import { useSyncExternalStore } from "react";

/** 화면 너비 조건. `matchMedia`가 없는 환경(테스트)에서는 조건을 만족하지 않는 것으로 본다. */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (notify) => {
      if (typeof window.matchMedia !== "function") return () => undefined;
      const list = window.matchMedia(query);
      list.addEventListener("change", notify);
      return () => {
        list.removeEventListener("change", notify);
      };
    },
    () => typeof window.matchMedia === "function" && window.matchMedia(query).matches,
    () => false,
  );
}

/** 이 너비(56.25rem = 900px) 이상이면 대화 목록을 왼쪽에 고정해 보인다. `conversations.css`와 같은 값이다. */
export const WIDE_LAYOUT_QUERY = "(min-width: 56.25rem)";
