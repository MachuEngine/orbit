import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConnectionsScreen } from "./ConnectionsScreen";
import { MailIndexSection } from "./MailIndexSection";
import { agoText, labelDisplayName, scopeDisplayName, syncErrorMessage } from "./text";

type Handler = (url: string, init?: RequestInit) => Response | Promise<Response>;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubApi(handler: Handler) {
  const mock = vi
    .fn()
    .mockImplementation((url: string, init?: RequestInit) => Promise.resolve(handler(url, init)));
  vi.stubGlobal("fetch", mock);
  return mock;
}

function requests(
  mock: ReturnType<typeof stubApi>,
): { url: string; method: string; init?: RequestInit }[] {
  return (mock.mock.calls as [string, RequestInit | undefined][]).map(([url, init]) => ({
    url,
    method: init?.method ?? "GET",
    ...(init === undefined ? {} : { init }),
  }));
}

const LABELS = {
  labels: [
    { id: "INBOX", name: "INBOX", kind: "system" },
    { id: "SENT", name: "SENT", kind: "system" },
    { id: "STARRED", name: "STARRED", kind: "system" },
    { id: "IMPORTANT", name: "IMPORTANT", kind: "system" },
    { id: "DRAFT", name: "DRAFT", kind: "system" },
    { id: "UNREAD", name: "UNREAD", kind: "system" },
    { id: "YELLOW_STAR", name: "YELLOW_STAR", kind: "system" },
    { id: "RED_BANG", name: "RED_BANG", kind: "system" },
    { id: "GREEN_CHECK", name: "GREEN_CHECK", kind: "system" },
    { id: "Label_1", name: "프로젝트 <img src=x onerror=alert(1)>", kind: "user" },
  ],
};

const NOW = new Date("2026-11-12T10:00:00Z");

function status(over: Record<string, unknown> = {}) {
  return {
    scopes: [],
    indexed_mail_count: 0,
    last_synced_at: null,
    status: "none",
    error_kind: null,
    sync_queued: false,
    ...over,
  };
}

const INBOX_SCOPE = {
  id: "s1",
  label_id: "INBOX",
  label_name: "INBOX",
  window_days: 90,
  synced: true,
};

function render_(props: Partial<React.ComponentProps<typeof MailIndexSection>> = {}) {
  return render(<MailIndexSection connectionId="c1" now={() => NOW} {...props} />);
}

beforeEach(() => {
  document.cookie = "orbit_csrf=csrf-test-value";
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.cookie = "orbit_csrf=; max-age=0";
});

describe("문구 도우미", () => {
  it("시스템 라벨은 한국어, 사용자 라벨은 원래 이름이다", () => {
    expect(labelDisplayName({ id: "INBOX", name: "INBOX", kind: "system" })).toBe("받은편지함");
    expect(labelDisplayName({ id: "SENT", name: "SENT", kind: "system" })).toBe("보낸편지함");
    expect(labelDisplayName({ id: "INBOX", name: "INBOX", kind: "user" })).toBe("INBOX");
    expect(labelDisplayName({ id: "Label_1", name: "프로젝트", kind: "user" })).toBe("프로젝트");
    expect(scopeDisplayName({ label_id: "INBOX", label_name: "INBOX" })).toBe("받은편지함");
    expect(scopeDisplayName({ label_id: "Label_1", label_name: "프로젝트" })).toBe("프로젝트");
  });

  it("경과 시간을 N분 전으로 쓴다", () => {
    const at = (min: number) => new Date(NOW.getTime() - min * 60_000).toISOString();
    expect(agoText(at(0), NOW)).toBe("방금 전");
    expect(agoText(at(7), NOW)).toBe("7분 전");
    expect(agoText(at(125), NOW)).toBe("2시간 전");
    expect(agoText(at(3 * 24 * 60), NOW)).toBe("3일 전");
  });

  it("모르는 오류 종류는 일반 문구로 바꾼다", () => {
    expect(syncErrorMessage("permission_revoked")).toContain("다시 연결");
    expect(syncErrorMessage("embedding_transient")).toContain("색인하는 서비스");
    expect(syncErrorMessage("zzz <b>")).not.toContain("zzz");
  });
});

describe("메일 검색 색인 영역", () => {
  it("설명과 라벨·기간 선택을 보이고, 라벨은 한국어 이름과 원래 이름이며 외부 문자열은 텍스트다", async () => {
    stubApi((url) => (url.endsWith("/labels") ? json(LABELS) : json(status())));
    const { container } = render_();
    expect(await screen.findByRole("heading", { name: "메일 검색 색인" })).toBeInTheDocument();
    expect(screen.getByText(/연결을 해제하면 저장한 메일도 함께 지워집니다/)).toBeInTheDocument();
    const select = await screen.findByLabelText("라벨");
    await waitFor(() => {
      expect(within(select).getAllByRole("option")).toHaveLength(6);
    });
    const names = within(select)
      .getAllByRole("option")
      .map((o) => o.textContent);
    expect(names).toEqual([
      "라벨을 고르세요",
      "받은편지함",
      "보낸편지함",
      "별표편지함",
      "중요편지함",
      "프로젝트 <img src=x onerror=alert(1)>",
    ]);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByLabelText("기간")).toHaveValue("90");
    expect(
      screen.getByText("아직 고른 범위가 없습니다. 라벨과 기간을 골라 추가하면 색인을 시작합니다."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "범위 추가" })).toBeDisabled();
  });

  it("범위를 추가하면 라벨 ID와 기간만 보내고 목록을 다시 읽어 대기로 보인다", async () => {
    let added = false;
    const mock = stubApi((url, init) => {
      if (url.endsWith("/labels")) return json(LABELS);
      if (url.endsWith("/index/scopes") && init?.method === "POST") {
        added = true;
        return json({ ...INBOX_SCOPE, synced: false }, 201);
      }
      return json(
        added
          ? status({
              scopes: [{ ...INBOX_SCOPE, synced: false }],
              status: "pending",
              sync_queued: true,
            })
          : status(),
      );
    });
    render_({ pollMs: 100000 });
    await userEvent.selectOptions(await screen.findByLabelText("라벨"), "INBOX");
    await userEvent.selectOptions(screen.getByLabelText("기간"), "365");
    await userEvent.click(screen.getByRole("button", { name: "범위 추가" }));
    expect(
      await screen.findByText("범위를 추가했습니다. 동기화를 시작합니다."),
    ).toBeInTheDocument();
    const post = requests(mock).find((r) => r.method === "POST");
    expect(post?.url).toBe("/api/connections/c1/index/scopes");
    expect(JSON.parse(post?.init?.body as string)).toEqual({ label_id: "INBOX", window_days: 365 });
    expect((post?.init?.headers as Record<string, string>)["X-CSRF-Token"]).toBe("csrf-test-value");
    const list = await screen.findByRole("list", { name: "색인 범위" });
    expect(within(list).getByText("받은편지함")).toBeInTheDocument();
    expect(within(list).getByText("대기")).toBeInTheDocument();
    expect(screen.getByText(/동기화하는 중입니다/)).toBeInTheDocument();
  });

  it("추가에 실패하면 이유와 다음 행동을 알리고, 모르는 라벨이면 목록을 다시 읽는다", async () => {
    const mock = stubApi((url, init) => {
      if (url.endsWith("/labels")) return json(LABELS);
      if (init?.method === "POST") return json({ detail: "unknown_label" }, 422);
      return json(status());
    });
    render_();
    await userEvent.selectOptions(await screen.findByLabelText("라벨"), "INBOX");
    await userEvent.click(screen.getByRole("button", { name: "범위 추가" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("이 라벨을 찾지 못했습니다");
    await waitFor(() => {
      expect(requests(mock).filter((r) => r.url.endsWith("/labels"))).toHaveLength(2);
    });
  });

  it("삭제는 확인 단계를 거치고, 취소하면 요청을 보내지 않으며, 확인하면 지운다", async () => {
    let deleted = false;
    const mock = stubApi((url, init) => {
      if (url.endsWith("/labels")) return json(LABELS);
      if (init?.method === "DELETE") {
        deleted = true;
        return json({ id: "s1" });
      }
      return json(deleted ? status() : status({ scopes: [INBOX_SCOPE], status: "ok" }));
    });
    render_();
    await userEvent.click(await screen.findByRole("button", { name: "받은편지함 범위 삭제" }));
    const group = screen.getByRole("group", { name: "범위 삭제 확인" });
    expect(group).toHaveTextContent("색인에 저장된 이 범위의 메일도 함께 지워집니다");
    await userEvent.click(within(group).getByRole("button", { name: "취소" }));
    expect(requests(mock).filter((r) => r.method === "DELETE")).toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: "받은편지함 범위 삭제" }));
    await userEvent.click(
      within(screen.getByRole("group", { name: "범위 삭제 확인" })).getByRole("button", {
        name: "범위 삭제",
      }),
    );
    expect(
      await screen.findByText("범위를 지웠습니다. 색인에서도 지웠습니다."),
    ).toBeInTheDocument();
    const del = requests(mock).find((r) => r.method === "DELETE");
    expect(del?.url).toBe("/api/connections/c1/index/scopes/s1");
    expect((del?.init?.headers as Record<string, string>)["X-CSRF-Token"]).toBe("csrf-test-value");
    expect(screen.queryByRole("list", { name: "색인 범위" })).toBeNull();
  });

  it("색인한 메일 수와 마지막 동기화를 N분 전으로 보이고 절대 시각은 title에 둔다", async () => {
    const last = new Date(NOW.getTime() - 7 * 60_000).toISOString();
    stubApi((url) =>
      url.endsWith("/labels")
        ? json(LABELS)
        : json(
            status({
              scopes: [INBOX_SCOPE],
              status: "ok",
              indexed_mail_count: 1234,
              last_synced_at: last,
            }),
          ),
    );
    render_();
    expect(await screen.findByText("1,234통")).toBeInTheDocument();
    const time = screen.getByText("7분 전");
    expect(time.tagName).toBe("TIME");
    expect(time).toHaveAttribute("title", expect.stringContaining("2026"));
    expect(screen.getByText("완료")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "지금 동기화" })).toBeEnabled();
  });

  it("동기화 오류는 종류별 문구와 기존 색인을 계속 쓸 수 있다는 안내를 보인다", async () => {
    stubApi((url) =>
      url.endsWith("/labels")
        ? json(LABELS)
        : json(
            status({
              scopes: [INBOX_SCOPE],
              status: "error",
              error_kind: "permission_revoked",
              last_synced_at: NOW.toISOString(),
              indexed_mail_count: 3,
            }),
          ),
    );
    render_();
    const alert = await screen.findByText(/권한이 거부돼 동기화하지 못했습니다/);
    expect(alert).toBeInTheDocument();
    expect(alert).toHaveTextContent("이미 저장한 메일로는 계속 찾을 수 있습니다");
  });

  it("범위가 없으면 지금 동기화를 막는다", async () => {
    stubApi((url) => (url.endsWith("/labels") ? json(LABELS) : json(status())));
    render_();
    expect(await screen.findByRole("button", { name: "지금 동기화" })).toBeDisabled();
  });

  it("지금 동기화를 누르면 POST를 보내고 이미 대기 중이면 그렇게 알린다", async () => {
    let queued = false;
    const mock = stubApi((url, init) => {
      if (url.endsWith("/labels")) return json(LABELS);
      if (url.endsWith("/index/sync") && init?.method === "POST") {
        const wasQueued = queued;
        queued = true;
        return json({ queued: !wasQueued }, 202);
      }
      return json(status({ scopes: [INBOX_SCOPE], status: "ok" }));
    });
    render_();
    await userEvent.click(await screen.findByRole("button", { name: "지금 동기화" }));
    expect(await screen.findByText("동기화를 요청했습니다.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "지금 동기화" }));
    expect(await screen.findByText("이미 동기화를 기다리고 있습니다.")).toBeInTheDocument();
    const posts = requests(mock).filter((r) => r.method === "POST");
    expect(posts).toHaveLength(2);
  });

  it("동기화 요청이 실패하면 다시 누르라고 알린다", async () => {
    stubApi((url, init) => {
      if (url.endsWith("/labels")) return json(LABELS);
      if (init?.method === "POST") return json({ detail: "x" }, 500);
      return json(status({ scopes: [INBOX_SCOPE], status: "ok" }));
    });
    render_();
    await userEvent.click(await screen.findByRole("button", { name: "지금 동기화" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("동기화를 요청하지 못했습니다");
  });

  it("불러오기에 실패하면 알리고 다시 불러올 수 있다", async () => {
    let fail = true;
    stubApi((url) => {
      if (url.endsWith("/labels")) return json(LABELS);
      return fail ? json({}, 500) : json(status());
    });
    render_();
    expect(await screen.findByText(/색인 정보를 불러오지 못했습니다/)).toBeInTheDocument();
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "다시 불러오기" }));
    expect(await screen.findByLabelText("라벨")).toBeInTheDocument();
  });

  it("라벨 조회가 커넥터 오류로 실패하면 종류별 문구와 다시 불러오기를 보인다", async () => {
    stubApi((url) =>
      url.endsWith("/labels") ? json({ detail: "rate_limited" }, 502) : json(status()),
    );
    render_();
    expect(await screen.findByText(/Google 요청 한도에 걸렸습니다/)).toBeInTheDocument();
    expect(screen.getByLabelText("라벨")).toBeDisabled();
    expect(screen.getByRole("button", { name: "다시 불러오기" })).toBeInTheDocument();
  });
});

describe("상태 폴링", () => {
  const statusCalls = (mock: ReturnType<typeof stubApi>) =>
    requests(mock).filter((r) => r.url.endsWith("/index")).length;

  it("동기화가 대기 중이면 다시 읽고, 끝나면 멈춘다", async () => {
    let remaining = 2;
    const mock = stubApi((url) => {
      if (url.endsWith("/labels")) return json(LABELS);
      const syncing = remaining > 0;
      remaining -= 1;
      return json(
        status({
          scopes: [{ ...INBOX_SCOPE, synced: !syncing }],
          status: syncing ? "pending" : "ok",
          sync_queued: syncing,
          indexed_mail_count: syncing ? 0 : 5,
          last_synced_at: syncing ? null : NOW.toISOString(),
        }),
      );
    });
    render_({ pollMs: 20 });
    expect(await screen.findByText(/동기화하는 중입니다/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "지금 동기화" })).toBeDisabled();
    expect(await screen.findByText("5통")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "지금 동기화" })).toBeEnabled();
    const settled = statusCalls(mock);
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(statusCalls(mock)).toBe(settled); // 끝난 뒤에는 더 읽지 않는다
  });

  it("동기화 중이 아니면 처음부터 다시 읽지 않는다", async () => {
    const mock = stubApi((url) =>
      url.endsWith("/labels")
        ? json(LABELS)
        : json(status({ scopes: [INBOX_SCOPE], status: "ok" })),
    );
    render_({ pollMs: 20 });
    await screen.findByText("완료");
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(statusCalls(mock)).toBe(1);
  });

  it("화면을 떠나면 폴링을 멈춘다", async () => {
    const mock = stubApi((url) =>
      url.endsWith("/labels")
        ? json(LABELS)
        : json(
            status({
              scopes: [{ ...INBOX_SCOPE, synced: false }],
              status: "pending",
              sync_queued: true,
            }),
          ),
    );
    const { unmount } = render_({ pollMs: 20 });
    await screen.findByText(/동기화하는 중입니다/);
    await waitFor(() => {
      expect(statusCalls(mock)).toBeGreaterThan(2);
    });
    unmount();
    const after = statusCalls(mock);
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(statusCalls(mock)).toBe(after);
  });

  it("폴링 중 한 번 실패해도 이미 읽은 화면을 지우지 않는다", async () => {
    let calls = 0;
    stubApi((url) => {
      if (url.endsWith("/labels")) return json(LABELS);
      calls += 1;
      return calls === 2
        ? json({}, 500)
        : json(
            status({
              scopes: [{ ...INBOX_SCOPE, synced: false }],
              status: "pending",
              sync_queued: true,
            }),
          );
    });
    render_({ pollMs: 20 });
    await screen.findByText(/동기화하는 중입니다/);
    await waitFor(() => {
      expect(calls).toBeGreaterThan(3);
    });
    expect(screen.queryByText(/불러오지 못했습니다/)).toBeNull();
    expect(screen.getByText(/동기화하는 중입니다/)).toBeInTheDocument();
  });
});

describe("폴링 보호", () => {
  const syncing = () =>
    status({ scopes: [{ ...INBOX_SCOPE, synced: false }], status: "pending", sync_queued: true });
  const statusCalls = (mock: ReturnType<typeof stubApi>) =>
    requests(mock).filter((r) => r.url.endsWith("/index")).length;

  it("앞선 요청이 끝나지 않았으면 다음 tick을 건너뛴다", async () => {
    let calls = 0;
    const mock = stubApi((url) => {
      if (url.endsWith("/labels")) return json(LABELS);
      calls += 1;
      if (calls === 1) return json(syncing());
      return new Promise<Response>((resolve) => {
        setTimeout(() => {
          resolve(json(syncing()));
        }, 200);
      });
    });
    render_({ pollMs: 20 });
    await screen.findByText(/동기화하는 중입니다/);
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(statusCalls(mock)).toBe(2); // 처음 읽기 + 아직 끝나지 않은 폴링 하나
  });

  it("연속 3회 실패하면 폴링을 멈추고 다시 불러오기 버튼을 보인다", async () => {
    let calls = 0;
    let recovered = false;
    const mock = stubApi((url) => {
      if (url.endsWith("/labels")) return json(LABELS);
      calls += 1;
      if (recovered) return json(status({ scopes: [INBOX_SCOPE], status: "ok" }));
      return calls === 1 ? json(syncing()) : json({}, 500);
    });
    render_({ pollMs: 20 });
    expect(await screen.findByText(/갱신을 멈췄습니다/)).toBeInTheDocument();
    const stopped = statusCalls(mock);
    expect(stopped).toBe(4); // 처음 1회 + 실패 3회
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(statusCalls(mock)).toBe(stopped);
    expect(screen.getByText(/동기화하는 중입니다/)).toBeInTheDocument(); // 읽은 화면은 그대로
    recovered = true;
    await userEvent.click(screen.getByRole("button", { name: "다시 불러오기" }));
    expect(await screen.findByText("완료")).toBeInTheDocument();
    expect(screen.queryByText(/갱신을 멈췄습니다/)).toBeNull();
  });

  it("중간에 성공하면 연속 실패 횟수를 다시 센다", async () => {
    let calls = 0;
    stubApi((url) => {
      if (url.endsWith("/labels")) return json(LABELS);
      calls += 1;
      return calls % 3 === 1 ? json(syncing()) : json({}, 500); // 성공 1회, 실패 2회 반복
    });
    render_({ pollMs: 20 });
    await waitFor(() => {
      expect(calls).toBeGreaterThan(9);
    });
    expect(screen.queryByText(/갱신을 멈췄습니다/)).toBeNull();
  });

  it("401이면 바로 멈추고 로그인 안내를 보이며 다시 불러오기는 없다", async () => {
    let calls = 0;
    const mock = stubApi((url) => {
      if (url.endsWith("/labels")) return json(LABELS);
      calls += 1;
      return calls === 1 ? json(syncing()) : json({ detail: "unauthorized" }, 401);
    });
    render_({ pollMs: 20 });
    expect(await screen.findByText(/로그인이 만료되었습니다/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "다시 불러오기" })).toBeNull();
    const stopped = statusCalls(mock);
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(statusCalls(mock)).toBe(stopped);
    expect(stopped).toBe(2);
  });
});

describe("연결 화면 안의 위치", () => {
  const connection = (status_: string) => ({
    id: "c1",
    provider: "google",
    status: status_,
    account_email: "alice@example.test",
    scopes: [],
    created_at: "2026-11-12T00:00:00Z",
  });
  const handler =
    (status_: string): Handler =>
    (url) => {
      if (url === "/api/connections") {
        return json({
          mode: "google",
          connect_available: true,
          connections: [connection(status_)],
        });
      }
      return url.endsWith("/labels") ? json(LABELS) : json(status());
    };

  it("연결이 active일 때만 보인다", async () => {
    stubApi(handler("active"));
    const { unmount } = render(<ConnectionsScreen result={null} onBack={() => undefined} />);
    expect(await screen.findByRole("heading", { name: "메일 검색 색인" })).toBeInTheDocument();
    unmount();
    stubApi(handler("error"));
    render(<ConnectionsScreen result={null} onBack={() => undefined} />);
    await screen.findByText("alice@example.test");
    expect(screen.queryByRole("heading", { name: "메일 검색 색인" })).toBeNull();
  });
});
