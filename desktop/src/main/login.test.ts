import http from "node:http";
import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  EXCHANGE_PATH,
  LoginController,
  START_PATH,
  exchangeCode,
  type ExchangeOutcome,
  type LoginDeps,
  type LoginEvent,
} from "./login";
import type { LoopbackReceiver, LoopbackResult } from "./loopback";
import { challengeFor } from "./pkce";

const ORIGIN = "http://localhost:5173";
const CODE = "C".repeat(43);

/** 결과를 테스트가 정하는 가짜 수신기. */
function fakeReceiver(port = 53000): {
  receiver: LoopbackReceiver;
  finish: (result: LoopbackResult) => void;
  cancelled: () => boolean;
} {
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
  let wasCancelled = false;
  return {
    receiver: {
      port,
      result,
      cancel: () => {
        wasCancelled = true;
        finish({ kind: "cancelled" });
      },
    },
    finish,
    cancelled: () => wasCancelled,
  };
}

function setup(overrides: Partial<LoginDeps> = {}) {
  const events: LoginEvent[] = [];
  const opened: string[] = [];
  const fake = fakeReceiver();
  let expectedState = "";
  const exchange = vi.fn<(code: string, verifier: string) => Promise<ExchangeOutcome>>(() =>
    Promise.resolve("ok"),
  );
  const reload = vi.fn();
  const deps: LoginDeps = {
    origin: ORIGIN,
    startReceiver: (options) => {
      expectedState = options.expectedState;
      return Promise.resolve(fake.receiver);
    },
    exchange,
    hasSessionCookie: () => Promise.resolve(true),
    openExternal: (href) => {
      opened.push(href);
    },
    reloadWindow: reload,
    emit: (event) => {
      events.push(event);
    },
    ...overrides,
  };
  return {
    controller: new LoginController(deps),
    events,
    opened,
    fake,
    exchange,
    reload,
    state: () => expectedState,
  };
}

const tick = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });

describe("LoginController", () => {
  it("PKCE·state를 만들어 시스템 브라우저로 시작 주소를 연다", async () => {
    const t = setup();
    void t.controller.start();
    await tick();
    const [href] = t.opened;
    const url = new URL(href ?? "");
    expect(url.origin).toBe(ORIGIN);
    expect(url.pathname).toBe(START_PATH);
    expect(url.searchParams.get("port")).toBe("53000");
    expect(url.searchParams.get("state")).toBe(t.state());
    expect(url.searchParams.get("code_challenge")).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect([...url.searchParams.keys()].sort()).toEqual(["code_challenge", "port", "state"]);
    t.fake.finish({ kind: "cancelled" });
  });

  it("콜백을 받으면 코드와 검증기로 교환하고 쿠키를 확인한 뒤 창을 다시 불러온다", async () => {
    const t = setup();
    const done = t.controller.start();
    await tick();
    t.fake.finish({ kind: "code", code: CODE });
    await done;
    expect(t.exchange).toHaveBeenCalledTimes(1);
    const [code, verifier] = t.exchange.mock.calls[0] ?? [];
    expect(code).toBe(CODE);
    // 시작 주소의 challenge는 교환에 쓰는 검증기에서 나온 값이다. 검증기는 주소에 없다.
    const url = new URL(t.opened[0] ?? "");
    expect(challengeFor(verifier ?? "")).toBe(url.searchParams.get("code_challenge"));
    expect(t.opened[0]).not.toContain(verifier ?? "?");
    expect(t.events).toEqual([{ status: "done" }]);
    expect(t.reload).toHaveBeenCalledTimes(1);
    // renderer로 가는 이벤트에는 코드도 검증기도 없다.
    expect(JSON.stringify(t.events)).not.toContain(CODE);
    expect(JSON.stringify(t.events)).not.toContain(verifier ?? "?");
  });

  it.each<[string, LoopbackResult, string]>([
    ["시간 초과", { kind: "timeout" }, "expired"],
    ["state 불일치", { kind: "state_mismatch" }, "state"],
    ["응답 형식 이상", { kind: "invalid" }, "exchange"],
    ["서버가 알린 오류", { kind: "error", reason: "not_allowed" }, "not_allowed"],
    ["사용자가 취소한 동의", { kind: "error", reason: "denied" }, "denied"],
  ])("%s이면 교환하지 않고 실패를 알린다", async (_name, result, reason) => {
    const t = setup();
    const done = t.controller.start();
    await tick();
    t.fake.finish(result);
    await done;
    expect(t.events).toEqual([{ status: "failed", reason }]);
    expect(t.exchange).not.toHaveBeenCalled();
    expect(t.reload).not.toHaveBeenCalled();
  });

  it("교환이 거절되거나 서버에 닿지 못하면 실패를 알리고 창을 다시 불러오지 않는다", async () => {
    for (const [outcome, reason] of [
      ["rejected", "exchange"],
      ["unavailable", "unavailable"],
    ] as const) {
      const t = setup({ exchange: () => Promise.resolve(outcome) });
      const done = t.controller.start();
      await tick();
      t.fake.finish({ kind: "code", code: CODE });
      await done;
      expect(t.events).toEqual([{ status: "failed", reason }]);
      expect(t.reload).not.toHaveBeenCalled();
    }
  });

  it("세션 쿠키가 저장되지 않았으면 실패로 알린다", async () => {
    const t = setup({ hasSessionCookie: () => Promise.resolve(false) });
    const done = t.controller.start();
    await tick();
    t.fake.finish({ kind: "code", code: CODE });
    await done;
    expect(t.events).toEqual([{ status: "failed", reason: "session" }]);
    expect(t.reload).not.toHaveBeenCalled();
  });

  it("수신기를 열지 못하면 unavailable로 알리고 브라우저를 열지 않는다", async () => {
    const t = setup({ startReceiver: () => Promise.reject(new Error("EADDRINUSE")) });
    await t.controller.start();
    expect(t.events).toEqual([{ status: "failed", reason: "unavailable" }]);
    expect(t.opened).toEqual([]);
  });

  it("새 시도가 시작되면 이전 수신기를 닫고 이전 시도는 이벤트를 보내지 않는다", async () => {
    const first = fakeReceiver(53001);
    const second = fakeReceiver(53002);
    const queue = [first, second];
    const t = setup({
      startReceiver: () => {
        const next = queue.shift();
        return next === undefined
          ? Promise.reject(new Error("no receiver"))
          : Promise.resolve(next.receiver);
      },
    });
    const run1 = t.controller.start();
    await tick();
    const run2 = t.controller.start();
    await tick();
    expect(first.cancelled()).toBe(true);
    await run1;
    expect(t.events).toEqual([]);
    second.finish({ kind: "code", code: CODE });
    await run2;
    expect(t.events).toEqual([{ status: "done" }]);
    expect(t.exchange).toHaveBeenCalledTimes(1);
  });

  it("cancel은 진행 중인 시도를 조용히 끝낸다", async () => {
    const t = setup();
    const done = t.controller.start();
    await tick();
    t.controller.cancel();
    await done;
    expect(t.fake.cancelled()).toBe(true);
    expect(t.events).toEqual([]);
  });
});

describe("exchangeCode (가짜 서버)", () => {
  const servers: http.Server[] = [];
  afterEach(() => {
    for (const server of servers.splice(0)) {
      server.close();
      server.closeAllConnections();
    }
  });

  async function fakeServer(status: number) {
    const seen: { method?: string; url?: string; type?: string; body: string }[] = [];
    const server = http.createServer((req, res) => {
      let body = "";
      req.on("data", (chunk: Buffer) => {
        body += chunk.toString();
      });
      req.on("end", () => {
        seen.push({ method: req.method, url: req.url, type: req.headers["content-type"], body });
        res.writeHead(status, { "Set-Cookie": "orbit_session=synthetic; HttpOnly" });
        res.end();
      });
    });
    servers.push(server);
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const origin = `http://127.0.0.1:${String((server.address() as AddressInfo).port)}`;
    return { origin, seen };
  }

  const realFetch = (url: string, init: Parameters<typeof fetch>[1]) => fetch(url, init);

  it("JSON 본문으로 POST하고 204면 ok다", async () => {
    const server = await fakeServer(204);
    const outcome = await exchangeCode(realFetch, server.origin, CODE, "V".repeat(43));
    expect(outcome).toBe("ok");
    expect(server.seen).toEqual([
      {
        method: "POST",
        url: EXCHANGE_PATH,
        type: "application/json",
        body: JSON.stringify({ code: CODE, code_verifier: "V".repeat(43) }),
      },
    ]);
  });

  it("400은 rejected, 5xx는 unavailable이다", async () => {
    const rejected = await fakeServer(400);
    expect(await exchangeCode(realFetch, rejected.origin, CODE, "V".repeat(43))).toBe("rejected");
    const broken = await fakeServer(503);
    expect(await exchangeCode(realFetch, broken.origin, CODE, "V".repeat(43))).toBe("unavailable");
  });

  it("연결에 실패하면 unavailable이다", async () => {
    const server = await fakeServer(204);
    server.seen.length = 0;
    const closed = server.origin;
    for (const s of servers.splice(0)) {
      s.close();
      s.closeAllConnections();
    }
    await new Promise<void>((resolve) => {
      setTimeout(resolve, 50);
    });
    expect(await exchangeCode(realFetch, closed, CODE, "V".repeat(43))).toBe("unavailable");
  });
});
