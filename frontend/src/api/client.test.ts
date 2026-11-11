import { describe, expect, it, vi } from "vitest";
import { z } from "zod";
import { ApiClient, readCookie } from "./client";
import { ApiError } from "./errors";
import { createRun, decideApproval, getApproval } from "./endpoints";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makeClient(fetchMock: ReturnType<typeof vi.fn>, csrf: string | null = "tok123") {
  return new ApiClient({
    fetch: fetchMock as unknown as typeof fetch,
    getCookie: (name) => (name === "orbit_csrf" ? (csrf ?? undefined) : undefined),
  });
}

function callInit(fetchMock: ReturnType<typeof vi.fn>): RequestInit {
  return fetchMock.mock.calls[0]?.[1] as RequestInit;
}

const schema = z.object({ ok: z.boolean() });

describe("ApiClient CSRF", () => {
  it("상태를 바꾸는 요청에 X-CSRF-Token을 붙이고 같은 출처 쿠키를 쓴다", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }));
    await makeClient(fetchMock).request("POST", "/api/x", { body: { a: 1 }, schema });
    const init = callInit(fetchMock);
    expect((init.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok123");
    expect(init.credentials).toBe("same-origin");
    expect(init.body).toBe('{"a":1}');
  });

  it("GET에는 CSRF 헤더를 붙이지 않는다", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }));
    await makeClient(fetchMock).request("GET", "/api/x", { schema });
    expect(callInit(fetchMock).headers).not.toHaveProperty("X-CSRF-Token");
  });

  it("쿠키가 없으면 헤더 없이 보내고 서버의 403을 csrf로 분류한다", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: "csrf" }, 403));
    const promise = makeClient(fetchMock, null).request("POST", "/api/x");
    await expect(promise).rejects.toMatchObject({ kind: "csrf" });
    expect(callInit(fetchMock).headers).not.toHaveProperty("X-CSRF-Token");
  });
});

describe("ApiClient 오류 분류", () => {
  it.each([
    [401, "unauthorized"],
    [403, "csrf"],
    [404, "not_found"],
    [409, "conflict"],
    [500, "server"],
    [422, "server"],
  ])("HTTP %i는 %s로 분류한다", async (status, kind) => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: "x" }, status));
    await expect(makeClient(fetchMock).request("GET", "/api/x", { schema })).rejects.toMatchObject({
      kind,
      status,
    });
  });

  it("fetch가 던지면 network다", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(makeClient(fetchMock).request("GET", "/api/x")).rejects.toMatchObject({
      kind: "network",
    });
  });

  it("409의 detail 문자열을 보존한다", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: "expired" }, 409));
    const error = await makeClient(fetchMock)
      .request("POST", "/api/x")
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).detail).toBe("expired");
  });

  it("JSON이 아닌 오류 본문이어도 분류한다", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("bad gateway", { status: 502 }));
    await expect(makeClient(fetchMock).request("GET", "/api/x")).rejects.toMatchObject({
      kind: "server",
      detail: null,
    });
  });
});

describe("readCookie", () => {
  it("값을 디코드하고, 잘못된 퍼센트 인코딩이면 undefined를 돌려준다", () => {
    document.cookie = "orbit_csrf=a%20b";
    expect(readCookie("orbit_csrf")).toBe("a b");
    document.cookie = "bad=%E0%A4%A";
    expect(readCookie("bad")).toBeUndefined();
    expect(readCookie("missing")).toBeUndefined();
  });
});

describe("런타임 검증", () => {
  it("스키마와 다른 응답은 invalid_response로 거부한다", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: "yes" }));
    await expect(makeClient(fetchMock).request("GET", "/api/x", { schema })).rejects.toMatchObject({
      kind: "invalid_response",
    });
  });

  it("JSON이 아닌 성공 응답도 거부한다", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("<html>", { status: 200 }));
    await expect(makeClient(fetchMock).request("GET", "/api/x", { schema })).rejects.toMatchObject({
      kind: "invalid_response",
    });
  });

  it("createRun은 정의하지 않은 run 상태를 거부한다", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ run_id: "r", conversation_id: "c", status: "exploded" }));
    await expect(createRun({ message: "안녕" }, makeClient(fetchMock))).rejects.toMatchObject({
      kind: "invalid_response",
    });
  });

  it("createRun은 올바른 응답을 돌려준다", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ run_id: "r", conversation_id: "c", status: "queued" }, 201),
      );
    await expect(createRun({ message: "안녕" }, makeClient(fetchMock))).resolves.toEqual({
      run_id: "r",
      conversation_id: "c",
      status: "queued",
    });
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/runs");
  });

  it("승인 상세는 정의하지 않은 출처 값을 거부한다", async () => {
    const body = {
      id: "a",
      run_id: "r",
      status: "pending",
      tool_name: "send_mail",
      args: {},
      summary: {},
      provenance: { "x@example.com": "telepathy" },
      expires_at: "2026-01-01T00:00:00+00:00",
      decided_at: null,
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(body));
    await expect(getApproval("a", makeClient(fetchMock))).rejects.toMatchObject({
      kind: "invalid_response",
    });
  });

  it("결정 요청은 /api/approvals/{id}/decision으로 본문을 보낸다", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: "approved" }));
    await decideApproval("a/b", "approve", makeClient(fetchMock));
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/approvals/a%2Fb/decision");
    expect(callInit(fetchMock).body).toBe('{"decision":"approve"}');
  });
});
