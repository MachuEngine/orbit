/**
 * 스모크 테스트: 실제 Electron 앱을 띄워 셸의 보안 설정과 상주 동작을 확인한다.
 * 서버는 127.0.0.1의 임의 포트에 띄운 테스트 페이지이고, 개발 서버(5173·8000)는 쓰지 않는다.
 * 전역 단축키는 끄고(ORBIT_DESKTOP_SHORTCUT=off) 임시 userData를 써서 실제 앱 설정과 섞이지 않게 한다.
 */
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import http from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import electronPath from "electron";
import { _electron as electron, type ElectronApplication, type Page } from "playwright-core";
import { createHash } from "node:crypto";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

interface TestServer {
  server: http.Server;
  origin: string;
  hits: string[];
  /** `POST /api/auth/desktop/exchange`로 받은 본문. */
  exchanges: string[];
  /** `POST /api/connections/google/desktop-complete`로 받은 요청. */
  completions: { csrf: string | undefined; cookie: string | undefined; body: string }[];
}

async function startServer(label: string, redirectTo?: () => string): Promise<TestServer> {
  const hits: string[] = [];
  const exchanges: string[] = [];
  const completions: TestServer["completions"] = [];
  const server = http.createServer((req, res) => {
    hits.push(req.url ?? "");
    if (req.url === "/api/auth/desktop/exchange" && req.method === "POST") {
      let body = "";
      req.on("data", (chunk: Buffer) => {
        body += chunk.toString();
      });
      req.on("end", () => {
        exchanges.push(body);
        // 서버가 앱 창에 세션을 주는 것과 같은 응답(합성 값).
        res.writeHead(204, {
          "Set-Cookie": [
            "orbit_session=synthetic-session; Path=/; HttpOnly; SameSite=Lax",
            "orbit_csrf=synthetic-csrf; Path=/; SameSite=Lax",
          ],
        });
        res.end();
      });
      return;
    }
    if (req.url === "/api/connections/google/desktop-complete" && req.method === "POST") {
      let body = "";
      req.on("data", (chunk: Buffer) => {
        body += chunk.toString();
      });
      req.on("end", () => {
        completions.push({
          csrf: req.headers["x-csrf-token"] as string | undefined,
          cookie: req.headers.cookie,
          body,
        });
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ result: "connected" }));
      });
      return;
    }
    if (req.url === "/redirect" && redirectTo !== undefined) {
      res.writeHead(302, { Location: redirectTo() });
      res.end();
      return;
    }
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    res.end(`<!doctype html><title>${label}</title><h1>${label}</h1>`);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  return { server, origin: `http://127.0.0.1:${String(port)}`, hits, exchanges, completions };
}

function stop(test: TestServer): Promise<void> {
  return new Promise((resolve) => {
    test.server.close(() => {
      resolve();
    });
  });
}

const sleep = (ms: number) =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, ms);
  });

let home: TestServer;
let other: TestServer;
let userData: string;
let app: ElectronApplication;
let page: Page;

function appEnv(origin: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const [key, value] of Object.entries(process.env)) if (value !== undefined) env[key] = value;
  return { ...env, ORBIT_DESKTOP_URL: origin, ORBIT_DESKTOP_SHORTCUT: "off" };
}

beforeAll(async () => {
  other = await startServer("other");
  home = await startServer("home", () => `${other.origin}/landed`);
  userData = mkdtempSync(path.join(tmpdir(), "orbit-desktop-smoke-"));
  app = await electron.launch({
    executablePath: electronPath as unknown as string,
    args: [`--user-data-dir=${userData}`, APP_DIR],
    env: appEnv(home.origin),
  });
  page = await app.firstWindow();
  await page.waitForLoadState("domcontentloaded");
});

afterAll(async () => {
  await app.close().catch(() => undefined);
  await stop(home);
  await stop(other);
  rmSync(userData, { recursive: true, force: true });
});

describe("window.orbitDesktop", () => {
  it("서버 화면을 열고 허용한 기능만 노출한다", async () => {
    expect(await page.title()).toBe("home");
    const exposed = await page.evaluate(() => {
      const w = window as unknown as Record<string, unknown>;
      const desktop = w.orbitDesktop as Record<string, unknown>;
      return {
        keys: Object.keys(desktop).sort(),
        types: Object.fromEntries(Object.entries(desktop).map(([k, v]) => [k, typeof v])),
        leaked: ["require", "process", "ipcRenderer", "electron", "Buffer"].filter(
          (name) => name in w,
        ),
      };
    });
    expect(exposed.keys).toEqual([
      "clearSession",
      "info",
      "notify",
      "onConnectEvent",
      "onLoginEvent",
      "onToggleVoice",
      "openConnectFlow",
      "prepareConnect",
      "setBadge",
      "startLogin",
    ]);
    expect(exposed.types).toEqual({
      clearSession: "function",
      info: "object",
      notify: "function",
      onConnectEvent: "function",
      onLoginEvent: "function",
      onToggleVoice: "function",
      openConnectFlow: "function",
      prepareConnect: "function",
      setBadge: "function",
      startLogin: "function",
    });
    expect(exposed.leaked).toEqual([]);
  });

  it("info에 플랫폼과 버전이 있다", async () => {
    const info = await page.evaluate(
      () =>
        (window as unknown as { orbitDesktop: { info: { platform: string; version: string } } })
          .orbitDesktop.info,
    );
    expect(info).toEqual({ platform: process.platform, version: "0.0.0" });
  });

  it("toggle-voice 이벤트를 받고 해제할 수 있다", async () => {
    await page.evaluate(() => {
      const w = window as unknown as {
        orbitDesktop: { onToggleVoice: (cb: () => void) => () => void };
        __toggles: number;
        __off: () => void;
      };
      w.__toggles = 0;
      w.__off = w.orbitDesktop.onToggleVoice(() => {
        w.__toggles += 1;
      });
    });
    const send = () =>
      app.evaluate(({ BrowserWindow }) => {
        BrowserWindow.getAllWindows()[0]?.webContents.send("orbit-desktop:toggle-voice");
      });
    const count = () => page.evaluate(() => (window as unknown as { __toggles: number }).__toggles);
    await send();
    await expect.poll(count).toBe(1);
    await page.evaluate(() => {
      (window as unknown as { __off: () => void }).__off();
    });
    await send();
    await sleep(300);
    expect(await count()).toBe(1);
  });

  it.runIf(process.platform === "darwin")("독 배지는 올바른 정수만 반영한다", async () => {
    const badge = () => app.evaluate(({ app: electronApp }) => electronApp.getBadgeCount());
    const set = (value: unknown) =>
      page.evaluate((v) => {
        (
          window as unknown as { orbitDesktop: { setBadge: (n: unknown) => void } }
        ).orbitDesktop.setBadge(v);
      }, value);
    await set(5);
    await expect.poll(badge).toBe(5);
    for (const invalid of [-1, 1.5, 1e9, "7"]) {
      await set(invalid);
      await sleep(150);
      expect(await badge()).toBe(5);
    }
    await set(0);
    await expect.poll(badge).toBe(0);
  });
});

describe("이동 제한", () => {
  const stay = () => new URL(page.url()).origin;

  it("다른 출처로 이동하면 막는다", async () => {
    await page.evaluate((url) => {
      window.location.href = url;
    }, `${other.origin}/direct`);
    await sleep(500);
    expect(stay()).toBe(home.origin);
    expect(other.hits).toEqual([]);
  });

  it("다른 출처로 가는 리디렉션을 막는다", async () => {
    await page.evaluate(() => {
      window.location.href = "/redirect";
    });
    await sleep(500);
    expect(stay()).toBe(home.origin);
    expect(other.hits).toEqual([]);
  });

  it("새 창을 열지 않는다", async () => {
    const opened = await page.evaluate(
      (url) => window.open(url, "_blank") !== null,
      `${other.origin}/popup`,
    );
    await sleep(300);
    expect(opened).toBe(false);
    expect(app.windows()).toHaveLength(1);
    expect(other.hits).toEqual([]);
  });

  it("허용하지 않는 권한(위치)은 거부한다", async () => {
    const code = await page.evaluate(
      () =>
        new Promise<number>((resolve) => {
          navigator.geolocation.getCurrentPosition(
            () => {
              resolve(0);
            },
            (error) => {
              resolve(error.code);
            },
          );
        }),
    );
    expect(code).toBe(1); // PERMISSION_DENIED
  });
});

describe("로그인·연결 핸드오프", () => {
  const TICKET = "T".repeat(43);

  /** 시스템 브라우저를 실제로 열지 않도록 main의 `shell.openExternal`을 기록용으로 바꾼다. */
  async function stubOpenExternal(): Promise<void> {
    await app.evaluate(({ shell }) => {
      const g = globalThis as unknown as { __opened: string[] };
      g.__opened = [];
      (shell as { openExternal: unknown }).openExternal = (url: string) => {
        g.__opened.push(url);
        return Promise.resolve();
      };
    });
  }
  const opened = () =>
    app.evaluate(() => (globalThis as unknown as { __opened: string[] }).__opened);
  const sessionCookies = () =>
    app.evaluate(async ({ session }) => {
      const cookies = await session.fromPartition("persist:orbit").cookies.get({
        name: "orbit_session",
      });
      return cookies.length;
    });

  beforeAll(async () => {
    // 앞 단계에서 창을 닫아 두었을 수 있다. 다시 열고 서버 화면이 로드된 상태로 맞춘다.
    await app.evaluate(({ BrowserWindow }) => {
      const win = BrowserWindow.getAllWindows()[0];
      win?.show();
      return win?.loadURL(win.webContents.getURL());
    });
    await page.waitForLoadState("domcontentloaded");
    await stubOpenExternal();
  });

  it("startLogin은 loopback 수신기를 열고 시스템 브라우저로 시작 주소를 연다", async () => {
    await page.evaluate(() => {
      (window as unknown as { orbitDesktop: { startLogin: () => void } }).orbitDesktop.startLogin();
    });
    await expect.poll(async () => (await opened()).length).toBe(1);
    const [href] = await opened();
    const url = new URL(href ?? "");
    expect(url.origin).toBe(home.origin);
    expect(url.pathname).toBe("/api/auth/desktop/start");
    expect(url.searchParams.get("code_challenge")).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(Number(url.searchParams.get("port"))).toBeGreaterThanOrEqual(1024);
    expect(url.searchParams.get("state")).toMatch(/^[A-Za-z0-9_-]{43}$/);
  });

  it("틀린 state의 콜백은 실패 이벤트로 알리고 교환하지 않는다", async () => {
    await page.evaluate(() => {
      const w = window as unknown as {
        orbitDesktop: { onLoginEvent: (cb: (e: unknown) => void) => () => void };
        __loginEvents: unknown[];
      };
      w.__loginEvents = [];
      w.orbitDesktop.onLoginEvent((event) => {
        w.__loginEvents.push(event);
      });
    });
    const url = new URL((await opened())[0] ?? "");
    const port = url.searchParams.get("port") ?? "";
    const reply = await fetch(
      `http://127.0.0.1:${port}/callback?code=${"C".repeat(43)}&state=wrong`,
    );
    expect(reply.status).toBe(400);
    await expect
      .poll(() =>
        page.evaluate(() => (window as unknown as { __loginEvents: unknown[] }).__loginEvents),
      )
      .toEqual([{ status: "failed", reason: "state" }]);
    expect(home.exchanges).toEqual([]);
  });

  it("올바른 콜백이면 main이 교환하고 세션 쿠키를 앱 파티션에 저장한 뒤 창을 다시 불러온다", async () => {
    await page.evaluate(() => {
      (window as unknown as { orbitDesktop: { startLogin: () => void } }).orbitDesktop.startLogin();
    });
    await expect.poll(async () => (await opened()).length).toBe(2);
    const url = new URL((await opened())[1] ?? "");
    const port = url.searchParams.get("port") ?? "";
    const state = url.searchParams.get("state") ?? "";
    const challenge = url.searchParams.get("code_challenge") ?? "";
    expect(await sessionCookies()).toBe(0);
    const hitsBefore = home.hits.filter((hit) => hit === "/").length;

    const code = "K".repeat(43);
    const reply = await fetch(`http://127.0.0.1:${port}/callback?code=${code}&state=${state}`);
    expect(reply.status).toBe(200);
    expect(await reply.text()).toContain("Orbit 앱으로 돌아가세요");

    await expect.poll(() => home.exchanges.length).toBe(1);
    const body = JSON.parse(home.exchanges[0] ?? "{}") as { code: string; code_verifier: string };
    expect(body.code).toBe(code);
    const expected = createHash("sha256").update(body.code_verifier, "ascii").digest("base64url");
    expect(expected).toBe(challenge);
    await expect.poll(sessionCookies).toBe(1);
    await expect
      .poll(() => home.hits.filter((hit) => hit === "/").length)
      .toBeGreaterThan(hitsBefore);
    // 수신기는 닫혔다.
    await expect(
      fetch(`http://127.0.0.1:${port}/callback?code=${code}&state=${state}`),
    ).rejects.toBeDefined();
  });

  it("openConnectFlow는 서버 출처의 desktop-begin 주소만 연다", async () => {
    const call = (value: unknown) =>
      page.evaluate((v) => {
        (
          window as unknown as { orbitDesktop: { openConnectFlow: (url: unknown) => void } }
        ).orbitDesktop.openConnectFlow(v);
      }, value);
    const before = (await opened()).length;
    for (const bad of [
      `${other.origin}/api/connections/google/desktop-begin?ticket=${TICKET}`,
      `${home.origin}/api/connections/google/start?ticket=${TICKET}`,
      `${home.origin}/api/connections/google/desktop-begin?ticket=short`,
      "https://accounts.google.com/o/oauth2/v2/auth",
      "file:///etc/passwd",
      42,
    ]) {
      await call(bad);
    }
    await sleep(300);
    expect((await opened()).length).toBe(before);
    const good = `${home.origin}/api/connections/google/desktop-begin?ticket=${TICKET}`;
    await call(good);
    await expect.poll(async () => (await opened()).length).toBe(before + 1);
    expect((await opened()).at(-1)).toBe(good);
  });

  it("prepareConnect는 challenge·포트·앱 state만 주고, 콜백 뒤 main이 앱 세션으로 완료를 제출한다", async () => {
    await page.evaluate(() => {
      const w = window as unknown as {
        orbitDesktop: { onConnectEvent: (cb: (e: unknown) => void) => () => void };
        __connectEvents: unknown[];
      };
      w.__connectEvents = [];
      w.orbitDesktop.onConnectEvent((event) => {
        w.__connectEvents.push(event);
      });
    });
    // 로그인 단계에서 받은 세션(쿠키)이 앱 파티션에 있어야 한다.
    const prepared = await page.evaluate(() =>
      (
        window as unknown as {
          orbitDesktop: { prepareConnect: (a: unknown) => Promise<Record<string, unknown>> };
        }
      ).orbitDesktop.prepareConnect({ intent: "connect" }),
    );
    expect(Object.keys(prepared).sort()).toEqual(["app_state", "code_challenge", "port"]);
    const challenge = String(prepared.code_challenge);
    const appState = String(prepared.app_state);
    const port = Number(prepared.port);
    expect(challenge).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(port).toBeGreaterThanOrEqual(1024);

    // 틀린 인자는 거부한다.
    await expect(
      page.evaluate(() =>
        (
          window as unknown as {
            orbitDesktop: { prepareConnect: (a: unknown) => Promise<unknown> };
          }
        ).orbitDesktop.prepareConnect({ intent: "other" }),
      ),
    ).rejects.toBeDefined();

    // 방금 거부된 호출은 수신기를 바꾸지 않는다. 올바른 콜백을 보낸다.
    const code = "Q".repeat(43);
    const reply = await fetch(
      `http://127.0.0.1:${String(port)}/callback?code=${code}&state=${appState}`,
    );
    expect(reply.status).toBe(200);
    await expect.poll(() => home.completions.length).toBe(1);
    const [completion] = home.completions;
    expect(completion?.csrf).toBe("synthetic-csrf");
    expect(completion?.cookie).toContain("orbit_session=synthetic-session");
    const body = JSON.parse(completion?.body ?? "{}") as { code: string; code_verifier: string };
    expect(body.code).toBe(code);
    expect(createHash("sha256").update(body.code_verifier, "ascii").digest("base64url")).toBe(
      challenge,
    );
    await expect
      .poll(() =>
        page.evaluate(() => (window as unknown as { __connectEvents: unknown[] }).__connectEvents),
      )
      .toEqual([{ status: "done", result: "connected" }]);
  });

  it("연결 콜백의 state가 틀리면 완료를 제출하지 않고 실패로 알린다", async () => {
    const prepared = await page.evaluate(() =>
      (
        window as unknown as {
          orbitDesktop: { prepareConnect: (a: unknown) => Promise<{ port: number }> };
        }
      ).orbitDesktop.prepareConnect({ intent: "docs_scope", connection_id: "c1" }),
    );
    const before = home.completions.length;
    const reply = await fetch(
      `http://127.0.0.1:${String(prepared.port)}/callback?code=${"Q".repeat(43)}&state=wrong`,
    );
    expect(reply.status).toBe(400);
    await expect
      .poll(() =>
        page.evaluate(
          () => (window as unknown as { __connectEvents: unknown[] }).__connectEvents.length,
        ),
      )
      .toBe(2);
    expect(
      await page.evaluate(
        () => (window as unknown as { __connectEvents: unknown[] }).__connectEvents[1],
      ),
    ).toEqual({ status: "failed", reason: "state" });
    expect(home.completions.length).toBe(before);
  });

  it("clearSession은 앱 파티션의 쿠키를 지운다", async () => {
    expect(await sessionCookies()).toBe(1);
    await page.evaluate(() => {
      (
        window as unknown as { orbitDesktop: { clearSession: () => void } }
      ).orbitDesktop.clearSession();
    });
    await expect.poll(sessionCookies).toBe(0);
  });
});

describe("상주", () => {
  it("창을 닫으면 숨기고 앱은 살아 있다", async () => {
    await app.evaluate(({ BrowserWindow }) => {
      BrowserWindow.getAllWindows()[0]?.close();
    });
    const state = await app.evaluate(({ BrowserWindow, app: electronApp }) => {
      const win = BrowserWindow.getAllWindows()[0];
      return {
        windows: BrowserWindow.getAllWindows().length,
        visible: win?.isVisible(),
        ready: electronApp.isReady(),
      };
    });
    expect(state).toEqual({ windows: 1, visible: false, ready: true });
    // 숨겨진 창도 "보이는 문서"로 남아야 30초 알림 조회가 멈추지 않는다(backgroundThrottling: false).
    expect(await page.evaluate(() => document.hidden)).toBe(false);
  });

  it("두 번째 실행은 기존 앱에 넘기고 끝난다. 기존 창이 다시 보인다", async () => {
    const child = spawn(
      electronPath as unknown as string,
      [`--user-data-dir=${userData}`, APP_DIR],
      {
        env: appEnv(home.origin),
        stdio: "ignore",
      },
    );
    const code = await new Promise<number | null>((resolve) => {
      const timer = setTimeout(() => {
        child.kill();
        resolve(-1);
      }, 30_000);
      child.on("exit", (exitCode) => {
        clearTimeout(timer);
        resolve(exitCode);
      });
    });
    expect(code).toBe(0);
    await expect
      .poll(() =>
        app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]?.isVisible()),
      )
      .toBe(true);
  });
});
