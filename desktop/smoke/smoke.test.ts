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
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

interface TestServer {
  server: http.Server;
  origin: string;
  hits: string[];
}

async function startServer(label: string, redirectTo?: () => string): Promise<TestServer> {
  const hits: string[] = [];
  const server = http.createServer((req, res) => {
    hits.push(req.url ?? "");
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
  return { server, origin: `http://127.0.0.1:${String(port)}`, hits };
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
    expect(exposed.keys).toEqual(["info", "notify", "onToggleVoice", "setBadge"]);
    expect(exposed.types).toEqual({
      info: "object",
      notify: "function",
      onToggleVoice: "function",
      setBadge: "function",
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
