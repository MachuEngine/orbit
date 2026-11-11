import { describe, expect, it } from "vitest";
import {
  actionLabel,
  formatRemaining,
  isExpired,
  isIrreversible,
  provenanceWarnings,
  remainingMs,
  type Clock,
} from "./logic";

const clockAt = (iso: string): Clock => ({ now: () => new Date(iso) });

describe("만료까지 남은 시간", () => {
  const expiresAt = "2026-11-11T10:05:00+00:00";

  it("주입한 시계 기준으로 남은 밀리초를 계산한다", () => {
    expect(remainingMs(expiresAt, clockAt("2026-11-11T10:01:40+00:00"))).toBe(200_000);
  });

  it("만료 시각이 지나면 0이고 만료로 본다", () => {
    const later = clockAt("2026-11-11T10:06:00+00:00");
    expect(remainingMs(expiresAt, later)).toBe(0);
    expect(isExpired(expiresAt, later)).toBe(true);
  });

  it("해석할 수 없는 시각은 만료로 취급한다", () => {
    expect(remainingMs("not-a-date", clockAt("2026-11-11T10:00:00+00:00"))).toBe(0);
  });

  it("남은 시간을 한국어로 쓴다", () => {
    expect(formatRemaining(200_000)).toBe("3분 20초");
    expect(formatRemaining(3_600_000)).toBe("1시간");
    expect(formatRemaining(1)).toBe("1초");
    expect(formatRemaining(0)).toBe("만료됨");
  });
});

describe("출처 경고", () => {
  it("external_content와 model만 경고하고 user·workspace_data는 제외한다", () => {
    expect(
      provenanceWarnings({
        "a@example.com": "user",
        "b@example.com": "workspace_data",
        "c@example.com": "external_content",
        "d@example.com": "model",
      }),
    ).toEqual([
      { email: "c@example.com", provenance: "external_content" },
      { email: "d@example.com", provenance: "model" },
    ]);
  });

  it("값이 없으면 경고도 없다", () => {
    expect(provenanceWarnings({})).toEqual([]);
  });
});

describe("동작 문구", () => {
  it("실제 동작과 같은 동사를 쓰고 메일 발송만 되돌릴 수 없다고 본다", () => {
    expect(actionLabel("send_mail")?.confirm).toBe("보내기");
    expect(actionLabel("update_event_time")?.confirm).toBe("일정 변경");
    expect(actionLabel("unknown_tool")).toBeNull();
    expect(isIrreversible("send_mail")).toBe(true);
    expect(isIrreversible("cancel_event")).toBe(false);
  });
});
