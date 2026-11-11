import { useEffect, useState } from "react";
import { remainingMs, systemClock, type Clock } from "./logic";

/** 만료까지 남은 밀리초를 `tickMs`마다 갱신한다. 0이 되면 갱신을 멈춘다. */
export function useRemainingMs(
  expiresAt: string,
  clock: Clock = systemClock,
  tickMs = 1000,
): number {
  const [remaining, setRemaining] = useState(() => remainingMs(expiresAt, clock));

  useEffect(() => {
    const update = () => {
      setRemaining(remainingMs(expiresAt, clock));
    };
    update();
    const timer = setInterval(update, tickMs);
    return () => {
      clearInterval(timer);
    };
  }, [expiresAt, clock, tickMs]);

  return remaining;
}
