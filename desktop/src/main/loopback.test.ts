import http from "node:http";
import { networkInterfaces } from "node:os";
import { afterEach, describe, expect, it } from "vitest";
import { startLoopbackReceiver, type LoopbackReceiver } from "./loopback";

const STATE = "S".repeat(43);
const CODE = "C".repeat(43);
const open: LoopbackReceiver[] = [];

async function receiver(options: { timeoutMs?: number } = {}): Promise<LoopbackReceiver> {
  const made = await startLoopbackReceiver({ expectedState: STATE, ...options });
  open.push(made);
  return made;
}

afterEach(() => {
  for (const item of open.splice(0)) item.cancel();
});

interface Reply {
  status: number;
  headers: http.IncomingHttpHeaders;
  body: string;
}

function request(
  port: number,
  path: string,
  options: { method?: string; headers?: Record<string, string>; host?: string } = {},
): Promise<Reply> {
  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        host: options.host ?? "127.0.0.1",
        port,
        path,
        method: options.method ?? "GET",
        headers: options.headers,
        agent: false,
      },
      (res) => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (chunk: string) => {
          body += chunk;
        });
        res.on("end", () => {
          resolve({ status: res.statusCode ?? 0, headers: res.headers, body });
        });
      },
    );
    req.on("error", reject);
    req.end();
  });
}

const callback = (port: number, query: string) => request(port, `/callback?${query}`);

describe("loopback 수신기", () => {
  it("127.0.0.1의 임의 포트에서 올바른 콜백 하나를 받고 안내 화면을 응답한다", async () => {
    const r = await receiver();
    expect(r.port).toBeGreaterThanOrEqual(1024);
    const reply = await callback(r.port, `code=${CODE}&state=${STATE}`);
    expect(reply.status).toBe(200);
    expect(reply.body).toContain("Orbit 앱으로 돌아가세요");
    expect(reply.headers["cache-control"]).toBe("no-store");
    expect(reply.headers["content-security-policy"]).toContain("default-src 'none'");
    expect(await r.result).toEqual({ kind: "code", code: CODE });
  });

  it("한 번 받으면 닫혀서 두 번째 요청은 연결되지 않는다", async () => {
    const r = await receiver();
    await callback(r.port, `code=${CODE}&state=${STATE}`);
    await r.result;
    await expect(callback(r.port, `code=${CODE}&state=${STATE}`)).rejects.toMatchObject({
      code: "ECONNREFUSED",
    });
  });

  it("127.0.0.1에만 바인딩한다(다른 네트워크 인터페이스 주소로는 연결되지 않는다)", async () => {
    const r = await receiver();
    const external = Object.values(networkInterfaces())
      .flat()
      .find((info) => info?.family === "IPv4" && !info.internal);
    if (external !== undefined) {
      await expect(
        request(r.port, `/callback?code=${CODE}&state=${STATE}`, { host: external.address }),
      ).rejects.toMatchObject({ code: "ECONNREFUSED" });
    }
    const local = await callback(r.port, `code=${CODE}&state=${STATE}`);
    expect(local.status).toBe(200);
  });

  it("다른 경로는 404로 거절하고 계속 기다린다", async () => {
    const r = await receiver();
    for (const path of ["/", "/callback/x", "/Callback", "/other?code=x"]) {
      expect((await request(r.port, path)).status).toBe(404);
    }
    expect((await callback(r.port, `code=${CODE}&state=${STATE}`)).status).toBe(200);
    expect(await r.result).toEqual({ kind: "code", code: CODE });
  });

  it("GET이 아닌 요청과 Host가 다른 요청은 거절하고 계속 기다린다", async () => {
    const r = await receiver();
    const post = await request(r.port, `/callback?code=${CODE}&state=${STATE}`, { method: "POST" });
    expect(post.status).toBe(405);
    const rebinding = await request(r.port, `/callback?code=${CODE}&state=${STATE}`, {
      headers: { Host: "evil.test" },
    });
    expect(rebinding.status).toBe(421);
    expect((await callback(r.port, `code=${CODE}&state=${STATE}`)).status).toBe(200);
    expect(await r.result).toEqual({ kind: "code", code: CODE });
  });

  it("state가 다르거나 없으면 실패로 끝내고 닫는다", async () => {
    const wrong = await receiver();
    const reply = await callback(wrong.port, `code=${CODE}&state=${"X".repeat(43)}`);
    expect(reply.status).toBe(400);
    expect(reply.body).not.toContain(CODE);
    expect(await wrong.result).toEqual({ kind: "state_mismatch" });
    await expect(callback(wrong.port, `code=${CODE}&state=${STATE}`)).rejects.toBeDefined();

    const missing = await receiver();
    await callback(missing.port, `code=${CODE}`);
    expect(await missing.result).toEqual({ kind: "state_mismatch" });
  });

  it("서버가 알린 오류 종류를 그대로 전달한다", async () => {
    const r = await receiver();
    expect((await callback(r.port, `error=not_allowed&state=${STATE}`)).status).toBe(400);
    expect(await r.result).toEqual({ kind: "error", reason: "not_allowed" });
  });

  it.each([
    ["코드 없음", `state=${STATE}`],
    ["짧은 코드", `code=abc&state=${STATE}`],
    ["허용하지 않는 문자", `code=${"C".repeat(42)}%2B&state=${STATE}`],
    ["이상한 오류 이름", `error=BAD-ERROR&state=${STATE}`],
    ["코드와 오류가 함께", `code=${CODE}&error=denied&state=${STATE}`],
  ])("응답 형식이 올바르지 않으면 실패로 끝낸다: %s", async (_name, query) => {
    const r = await receiver();
    expect((await callback(r.port, query)).status).toBe(400);
    const result = await r.result;
    expect(["invalid", "error"]).toContain(result.kind);
    expect(result.kind === "code").toBe(false);
  });

  it("제한 시간이 지나면 timeout으로 끝내고 닫는다", async () => {
    const r = await receiver({ timeoutMs: 80 });
    expect(await r.result).toEqual({ kind: "timeout" });
    await expect(callback(r.port, `code=${CODE}&state=${STATE}`)).rejects.toMatchObject({
      code: "ECONNREFUSED",
    });
  });

  it("취소하면 cancelled로 끝내고, 이미 끝난 뒤의 취소는 결과를 바꾸지 않는다", async () => {
    const a = await receiver();
    a.cancel();
    expect(await a.result).toEqual({ kind: "cancelled" });
    const b = await receiver();
    await callback(b.port, `code=${CODE}&state=${STATE}`);
    b.cancel();
    expect(await b.result).toEqual({ kind: "code", code: CODE });
  });
});
