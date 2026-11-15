import http from "node:http";
import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  COMPLETE_PATH,
  ConnectController,
  completeConnect,
  type CompleteFetch,
  type CompleteOutcome,
  type ConnectDeps,
  type ConnectEvent,
} from "./connect";
import type { LoopbackReceiver, LoopbackResult } from "./loopback";
import { challengeFor } from "./pkce";
import { parsePrepareConnectArgs } from "./validate";

const CODE = "C".repeat(43);

function fakeReceiver(port = 53100) {
  let resolve: (result: LoopbackResult) => void = () => undefined;
  let settled = false;
  const result = new Promise<LoopbackResult>((r) => {
    resolve = r;
  });
  const finish = (value: LoopbackResult) => {
    if (settled) return;
    settled = true;
    resolve(value);
  };
  let cancelled = false;
  const receiver: LoopbackReceiver = {
    port,
    result,
    cancel: () => {
      cancelled = true;
      finish({ kind: "cancelled" });
    },
  };
  return { receiver, finish, cancelled: () => cancelled };
}

const tick = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });

function setup(overrides: Partial<ConnectDeps> = {}) {
  const events: ConnectEvent[] = [];
  const fake = fakeReceiver();
  let expectedState = "";
  const complete = vi.fn<(code: string, verifier: string) => Promise<CompleteOutcome>>(() =>
    Promise.resolve({ kind: "done", result: "connected" }),
  );
  const controller = new ConnectController({
    startReceiver: (options) => {
      expectedState = options.expectedState;
      return Promise.resolve(fake.receiver);
    },
    complete,
    emit: (event) => {
      events.push(event);
    },
    ...overrides,
  });
  return { controller, events, fake, complete, state: () => expectedState };
}

describe("ConnectController", () => {
  it("challenge·포트·앱 state만 돌려주고 검증기는 renderer에 주지 않는다", async () => {
    const t = setup();
    const prepared = await t.controller.prepare();
    expect(Object.keys(prepared).sort()).toEqual(["app_state", "code_challenge", "port"]);
    expect(prepared.port).toBe(53100);
    expect(prepared.app_state).toBe(t.state());
    expect(prepared.code_challenge).toMatch(/^[A-Za-z0-9_-]{43}$/);
    t.fake.finish({ kind: "cancelled" });
  });

  it("콜백을 받으면 코드와 검증기로 완료를 제출하고 결과를 알린다", async () => {
    const t = setup();
    const prepared = await t.controller.prepare();
    t.fake.finish({ kind: "code", code: CODE });
    await tick();
    expect(t.complete).toHaveBeenCalledTimes(1);
    const [code, verifier] = t.complete.mock.calls[0] ?? [];
    expect(code).toBe(CODE);
    expect(challengeFor(verifier ?? "")).toBe(prepared.code_challenge);
    expect(t.events).toEqual([{ status: "done", result: "connected" }]);
    expect(JSON.stringify([prepared, t.events])).not.toContain(verifier ?? "?");
  });

  it("서버가 알린 저장 규칙 위반을 실패 이벤트로 알린다", async () => {
    const t = setup({
      complete: () => Promise.resolve({ kind: "failed", reason: "account_mismatch" }),
    });
    await t.controller.prepare();
    t.fake.finish({ kind: "code", code: CODE });
    await tick();
    expect(t.events).toEqual([{ status: "failed", reason: "account_mismatch" }]);
  });

  it.each<[string, LoopbackResult, string]>([
    ["시간 초과", { kind: "timeout" }, "expired"],
    ["state 불일치", { kind: "state_mismatch" }, "state"],
    ["응답 형식 이상", { kind: "invalid" }, "exchange"],
    ["동의 거절", { kind: "error", reason: "denied" }, "denied"],
  ])("%s이면 완료를 제출하지 않고 실패를 알린다", async (_name, result, reason) => {
    const t = setup();
    await t.controller.prepare();
    t.fake.finish(result);
    await tick();
    expect(t.events).toEqual([{ status: "failed", reason }]);
    expect(t.complete).not.toHaveBeenCalled();
  });

  it("새 prepare는 이전 수신기를 닫고 이전 시도는 이벤트를 보내지 않는다", async () => {
    const first = fakeReceiver(53101);
    const second = fakeReceiver(53102);
    const queue = [first, second];
    const t = setup({
      startReceiver: () => {
        const next = queue.shift();
        return next === undefined
          ? Promise.reject(new Error("none"))
          : Promise.resolve(next.receiver);
      },
    });
    await t.controller.prepare();
    await t.controller.prepare();
    await tick();
    expect(first.cancelled()).toBe(true);
    expect(t.events).toEqual([]);
    second.finish({ kind: "code", code: CODE });
    await tick();
    expect(t.events).toEqual([{ status: "done", result: "connected" }]);
  });

  it("수신기를 열지 못하면 prepare가 실패한다", async () => {
    const t = setup({ startReceiver: () => Promise.reject(new Error("EADDRINUSE")) });
    await expect(t.controller.prepare()).rejects.toThrow();
  });

  it("cancel은 조용히 끝낸다", async () => {
    const t = setup();
    await t.controller.prepare();
    t.controller.cancel();
    await tick();
    expect(t.fake.cancelled()).toBe(true);
    expect(t.events).toEqual([]);
  });
});

describe("completeConnect (가짜 서버)", () => {
  const servers: http.Server[] = [];
  afterEach(() => {
    for (const server of servers.splice(0)) {
      server.close();
      server.closeAllConnections();
    }
  });

  async function fakeServer(status: number, body: unknown) {
    const seen: { method?: string; url?: string; csrf?: string; type?: string; body: string }[] =
      [];
    const server = http.createServer((req, res) => {
      let data = "";
      req.on("data", (chunk: Buffer) => {
        data += chunk.toString();
      });
      req.on("end", () => {
        seen.push({
          method: req.method,
          url: req.url,
          csrf: req.headers["x-csrf-token"] as string | undefined,
          type: req.headers["content-type"],
          body: data,
        });
        res.writeHead(status, { "Content-Type": "application/json" });
        res.end(JSON.stringify(body));
      });
    });
    servers.push(server);
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    return { origin: `http://127.0.0.1:${String((server.address() as AddressInfo).port)}`, seen };
  }

  const realFetch: CompleteFetch = (url, init) => fetch(url, init);

  it("CSRF 헤더와 JSON 본문으로 POST하고 결과를 읽는다", async () => {
    const server = await fakeServer(200, { result: "docs_added" });
    const outcome = await completeConnect(realFetch, server.origin, "csrf-1", CODE, "V".repeat(43));
    expect(outcome).toEqual({ kind: "done", result: "docs_added" });
    expect(server.seen).toEqual([
      {
        method: "POST",
        url: COMPLETE_PATH,
        csrf: "csrf-1",
        type: "application/json",
        body: JSON.stringify({ code: CODE, code_verifier: "V".repeat(43) }),
      },
    ]);
  });

  it("CSRF 쿠키가 없으면 서버를 부르지 않고 session 실패다", async () => {
    const server = await fakeServer(200, { result: "connected" });
    for (const token of [null, ""]) {
      expect(await completeConnect(realFetch, server.origin, token, CODE, "V")).toEqual({
        kind: "failed",
        reason: "session",
      });
    }
    expect(server.seen).toEqual([]);
  });

  it.each<[number, unknown, CompleteOutcome]>([
    [200, { result: "error", error: "scope_missing" }, { kind: "failed", reason: "scope_missing" }],
    [200, { result: "error", error: "BAD NAME" }, { kind: "failed", reason: "exchange" }],
    [200, { result: "other" }, { kind: "failed", reason: "exchange" }],
    [200, ["x"], { kind: "failed", reason: "exchange" }],
    [400, { detail: "invalid_grant" }, { kind: "failed", reason: "exchange" }],
    [401, { detail: "unauthenticated" }, { kind: "failed", reason: "session" }],
    [403, { detail: "csrf_mismatch" }, { kind: "failed", reason: "session" }],
    [503, { detail: "unavailable" }, { kind: "failed", reason: "unavailable" }],
  ])("응답 %i %j를 알맞은 결과로 바꾼다", async (status, body, expected) => {
    const server = await fakeServer(status, body);
    expect(await completeConnect(realFetch, server.origin, "csrf", CODE, "V".repeat(43))).toEqual(
      expected,
    );
  });

  it("연결에 실패하면 unavailable이다", async () => {
    const closed = await fakeServer(200, {});
    for (const s of servers.splice(0)) {
      s.close();
      s.closeAllConnections();
    }
    await new Promise<void>((resolve) => {
      setTimeout(resolve, 50);
    });
    expect(await completeConnect(realFetch, closed.origin, "csrf", CODE, "V")).toEqual({
      kind: "failed",
      reason: "unavailable",
    });
  });
});

describe("parsePrepareConnectArgs", () => {
  it.each([
    [{ intent: "connect" }, "connect"],
    [{ intent: "docs_scope", connection_id: "c1" }, "docs_scope"],
  ])("올바른 인자는 intent만 돌려준다", (value, intent) => {
    expect(parsePrepareConnectArgs(value)).toEqual({ intent });
  });

  it.each([
    null,
    "connect",
    [],
    {},
    { intent: "other" },
    { intent: "connect", extra: 1 },
    { intent: "connect", connection_id: 5 },
    { intent: "connect", connection_id: "x".repeat(65) },
  ])("올바르지 않은 인자 %j는 거부한다", (value) => {
    expect(parsePrepareConnectArgs(value)).toBeNull();
  });
});
