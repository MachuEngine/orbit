import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import {
  decideApproval,
  getApproval,
  getConnections,
  getIndexStatus,
  getMailLabels,
  getConversation,
  getNotificationSettings,
  getNotifications,
  getSession,
  getUpcomingNotifications,
  listConversations,
  markAllNotificationsRead,
  createRun,
} from "../api/endpoints";
import { parseRunEvent } from "../runs/events";
import { PreviewBackend, installPreviewBackend } from "./fakeBackend";
import { SCENES, buildSceneData, findScene } from "./scenes";

// 고정 시각: 미리보기 데이터가 실행 시각에 따라 달라지지 않는지 확인한다.
const NOW = Date.parse("2026-11-13T09:00:00+09:00");

function clientFor(backend: PreviewBackend): ApiClient {
  return new ApiClient({ fetch: backend.fetch, getCookie: () => undefined });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("화면 미리보기 가짜 응답", () => {
  it.each(SCENES)("$id 장면의 응답이 모두 zod 스키마를 통과한다", async (scene) => {
    const data = buildSceneData(scene, NOW);
    const backend = new PreviewBackend(data);
    const client = clientFor(backend);

    // 로그인 화면 장면은 로그아웃 상태이거나 세션 확인이 실패한다(아래 로그인 장면 테스트).
    if (scene.login === undefined) expect((await getSession(client)).authenticated).toBe(true);
    const connections = await getConnections(client);
    const google = scene.needsConnection === true || scene.connections !== undefined;
    expect(connections.mode).toBe(google ? "google" : "sim");
    if (scene.needsConnection === true) expect(connections.connections).toHaveLength(0);
    // 계정 연결 장면: 연결마다 색인 상태와 라벨 목록이 스키마를 통과한다(색인 오류 장면은 500이다).
    if (scene.connections !== undefined) {
      expect(connections.connections.length).toBeGreaterThan(0);
      for (const item of connections.connections) {
        expect((await getMailLabels(item.id, client)).length).toBeGreaterThan(0);
        if (scene.connections === "error") {
          await expect(getIndexStatus(item.id, client)).rejects.toThrow();
        } else {
          const status = await getIndexStatus(item.id, client);
          expect(status.scopes.length).toBeGreaterThanOrEqual(2);
        }
      }
    }

    // 목록 응답이 오지 않거나(pending) 500인(error) 장면은 목록 요청을 따로 확인한다(아래 테스트).
    if (scene.list !== "pending" && scene.list !== "error") {
      const list = await listConversations(50, client);
      if (scene.list === "empty") {
        expect(list.conversations).toHaveLength(0);
      } else {
        expect(list.conversations.length).toBeGreaterThanOrEqual(3);
        expect(list.conversations.length).toBeLessThanOrEqual(5);
      }
      for (const item of list.conversations) {
        const detail = await getConversation(item.id, client);
        expect(detail.id).toBe(item.id);
      }
    }
    for (const id of Object.keys(data.approvals)) {
      expect((await getApproval(id, client)).id).toBe(id);
    }
    const arrived = await getNotifications(client);
    expect(arrived.unread_count).toBe(arrived.notifications.filter((n) => !n.read).length);
    expect((await getUpcomingNotifications(client)).length).toBeGreaterThan(0);
    expect((await getNotificationSettings(client)).lead_minutes).toBeGreaterThan(0);
  });

  it("사이드바 장면: 목록은 비어 있거나(empty), 500이거나(error), 응답이 오지 않는다(pending)", async () => {
    const empty = new PreviewBackend(buildSceneData(findScene("sidebar-empty"), NOW));
    expect(empty.handle("GET", "/api/conversations?limit=50", undefined).status).toBe(200);
    expect((await listConversations(50, clientFor(empty))).conversations).toHaveLength(0);

    const error = new PreviewBackend(buildSceneData(findScene("sidebar-error"), NOW));
    expect(error.handle("GET", "/api/conversations?limit=50", undefined).status).toBe(500);

    const pending = new PreviewBackend(buildSceneData(findScene("sidebar-loading"), NOW));
    const settled = vi.fn();
    void pending.fetch("/api/conversations?limit=50").then(settled);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(settled).not.toHaveBeenCalled();
    // 다른 요청은 그대로 응답한다.
    expect((await pending.fetch("/api/session")).status).toBe(200);
  });

  it("계정 연결 장면: 문서 권한·동기화 상태가 장면마다 다르다", async () => {
    const load = async (id: Parameters<typeof findScene>[0]) => {
      const backend = new PreviewBackend(buildSceneData(findScene(id), NOW));
      const client = clientFor(backend);
      const { connections } = await getConnections(client);
      const first = connections[0];
      return { first, status: first === undefined ? null : await getIndexStatus(first.id, client) };
    };
    expect((await load("connections-connected")).first?.docs_granted).toBe(true);
    expect((await load("connections-docs-off")).first?.docs_granted).toBe(false);
    const indexing = (await load("connections-indexing")).status;
    expect(indexing?.sync_queued).toBe(true);
    expect(indexing?.scopes.some((scope) => !scope.synced)).toBe(true);
    const none = new PreviewBackend(buildSceneData(findScene("connections-none"), NOW));
    expect((await getConnections(clientFor(none))).connections).toHaveLength(0);
    const broken = new PreviewBackend(buildSceneData(findScene("connections-error"), NOW));
    expect(broken.handle("GET", "/api/connections/conn-preview/index", undefined).status).toBe(500);
  });

  it("로그인 장면: 로그아웃 세션, 개발용 로그인 실패, 세션 확인 실패가 응답으로 만들어진다", async () => {
    const out = new PreviewBackend(buildSceneData(findScene("login-signed-out"), NOW));
    const session = await getSession(clientFor(out));
    expect(session.authenticated).toBe(false);
    expect(session.dev_login).toBe(true);
    expect(out.handle("POST", "/api/dev/login", {}).status).toBe(204);

    const failed = new PreviewBackend(buildSceneData(findScene("login-error"), NOW));
    expect((await getSession(clientFor(failed))).authenticated).toBe(false);
    expect(failed.handle("POST", "/api/dev/login", {}).status).toBe(500);

    const broken = new PreviewBackend(buildSceneData(findScene("login-connection-error"), NOW));
    expect(broken.handle("GET", "/api/session", undefined).status).toBe(500);
    await expect(getSession(clientFor(broken))).rejects.toThrow();
  });

  it("실시간 이벤트가 화면의 이벤트 스키마를 통과하고 승인 이벤트가 승인 상세와 이어진다", () => {
    const data = buildSceneData(findScene("chat-approval"), NOW);
    const all = [
      ...Object.values(data.runEvents).flat(),
      ...new PreviewBackend(data).eventsFor("run-new-1"),
    ];
    for (const [index, event] of all.entries()) {
      const parsed = parseRunEvent("1", event.type, JSON.stringify(event.payload));
      expect(parsed.kind, `${String(index)} ${event.type}`).toBe("event");
    }
    for (const events of Object.values(data.runEvents)) {
      for (const event of events) {
        if (event.type !== "approval_requested") continue;
        const approvalId = String(event.payload.approval_id);
        // 결과만 오는 승인(이미 끝난 것)은 상세가 없어도 된다. 대기 중인 것은 상세가 있어야 한다.
        const waits = events.at(-1)?.payload.status === "waiting_approval";
        if (waits) expect(data.approvals[approvalId]).toBeDefined();
      }
    }
  });

  it("승인 카드에 처음 보는 수신자 경고가 나오도록 출처가 외부 본문인 주소가 있다", () => {
    const data = buildSceneData(findScene("chat-approval"), NOW);
    const approval = data.approvals["ap-preview-mail"];
    expect(Object.values(approval?.provenance ?? {})).toContain("external_content");
  });

  it("실제 메일 주소·외부 주소를 쓰지 않는다(예약 도메인 example.com만)", () => {
    const text = JSON.stringify(SCENES.map((scene) => buildSceneData(scene, NOW)));
    for (const email of text.matchAll(/[\w.+-]+@([\w.-]+)/g)) {
      expect(email[1], email[0]).toBe("example.com");
    }
    expect(text).not.toMatch(/https?:\/\//);
  });

  it("쓰기 요청은 이 인스턴스 안에서만 반영하고 모르는 경로는 404다", async () => {
    const backend = new PreviewBackend(buildSceneData(findScene("chat-conversation"), NOW));
    const client = clientFor(backend);
    await markAllNotificationsRead(client);
    expect((await getNotifications(client)).unread_count).toBe(0);
    expect((await decideApproval("ap-preview-mail", "reject", client)).status).toBe("rejected");
    expect((await createRun({ message: "안녕" }, client)).run_id).toBe("run-new-1");
    expect(backend.handle("GET", "/api/unknown", undefined).status).toBe(404);
    expect(backend.handle("GET", "/api/conversations/none", undefined).status).toBe(404);
  });

  it("설치하면 window.fetch가 가짜로 바뀌어 실제 네트워크를 쓰지 않는다", async () => {
    const real = vi.fn().mockRejectedValue(new Error("네트워크 요청이 나갔다"));
    vi.stubGlobal("fetch", real);
    installPreviewBackend(new PreviewBackend(buildSceneData(findScene("chat-empty"), NOW)));
    const response = await window.fetch("/api/session");
    expect(response.status).toBe(200);
    expect(real).not.toHaveBeenCalled();
  });

  it("모르는 장면 이름은 빈 화면 장면으로 돌아간다", () => {
    expect(findScene("nope").id).toBe("chat-empty");
    expect(findScene(null).id).toBe("chat-empty");
  });
});
