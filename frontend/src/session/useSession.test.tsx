import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { sessionSchema } from "../api/schemas";
import { useSession } from "./useSession";

function Probe() {
  const { state } = useSession();
  return <output>{state.phase}</output>;
}

const base = { display_name: null, email: null, dev_login: true, google_login: true };

function stubSession(body: unknown, status = 200) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("sessionSchema", () => {
  it("authenticated가 없는 응답은 거부한다", () => {
    expect(sessionSchema.safeParse({}).success).toBe(false);
  });

  it("로그인 수단 필드가 없는 응답은 거부한다", () => {
    const old = { authenticated: false, workspace_id: null, expires_at: null };
    expect(sessionSchema.safeParse(old).success).toBe(false);
  });
});

describe("useSession", () => {
  it("authenticated=false면 signed_out이다", async () => {
    stubSession({ ...base, authenticated: false, workspace_id: null, expires_at: null });
    render(<Probe />);
    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("signed_out");
    });
  });

  it("authenticated=true면 signed_in이다", async () => {
    stubSession({
      ...base,
      authenticated: true,
      workspace_id: "w1",
      expires_at: "2026-11-12T00:00:00+00:00",
    });
    render(<Probe />);
    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("signed_in");
    });
  });

  it("형태가 다른 응답은 error다", async () => {
    stubSession({ ok: true });
    render(<Probe />);
    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("error");
    });
  });
});
