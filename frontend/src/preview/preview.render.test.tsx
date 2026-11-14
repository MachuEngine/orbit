import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { PreviewBackend, installPreviewBackend } from "./fakeBackend";
import { runSceneActions } from "./sceneActions";
import { buildSceneData, findScene, type SceneId } from "./scenes";

const realEventSource = Object.getOwnPropertyDescriptor(window, "EventSource");

/** jsdom에는 matchMedia가 없다. 화면 너비 조건을 흉내 낸다. */
function stubViewport(wide: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockImplementation((query: string) => ({
      matches: wide,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
}

function show(id: SceneId) {
  const scene = findScene(id);
  vi.stubGlobal("fetch", vi.fn());
  installPreviewBackend(new PreviewBackend(buildSceneData(scene, Date.now())));
  render(<App />);
  runSceneActions(scene);
}

afterEach(() => {
  vi.unstubAllGlobals();
  if (realEventSource === undefined) Reflect.deleteProperty(window, "EventSource");
  else Object.defineProperty(window, "EventSource", realEventSource);
});

describe("화면 미리보기 장면을 앱에 그대로 띄운다", () => {
  it("chat-empty: 빈 화면 안내와 대화 목록 버튼이 보인다", async () => {
    show("chat-empty");
    expect(
      await screen.findByText("메일, 일정, 문서에 대해 물어보세요. 예: “내일 일정 알려 줘”"),
    ).toBeInTheDocument();
  });

  it("chat-conversation: 비서 답변(마크다운)과 처리 결과 노트가 보인다", async () => {
    show("chat-conversation");
    expect(await screen.findByText("일정 시간을 변경했습니다.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "자료 위치" })).toBeInTheDocument();
    expect(screen.getByText("(docs.example.com/design-review)")).toBeInTheDocument();
  });

  it("chat-approval: 승인 카드에 처음 보는 수신자 경고가 보인다", async () => {
    show("chat-approval");
    const card = await screen.findByRole("region", { name: "메일 보내기" });
    expect(await within(card).findByText("partner@example.com")).toBeInTheDocument();
    expect(within(card).getByText("외부 출처")).toBeInTheDocument();
    expect(within(card).getByText("되돌릴 수 없음")).toBeInTheDocument();
  });

  it("chat-needs-connection: 연결 필요 배너가 보인다", async () => {
    show("chat-needs-connection");
    expect(await screen.findByRole("button", { name: "계정 연결하기" })).toBeInTheDocument();
  });

  it("chat-working: 응답 작성 중 상태가 보인다", async () => {
    show("chat-working");
    expect(await screen.findByText("작업 중입니다.")).toBeInTheDocument();
  });

  it("chat-notifications: 알림 패널이 열려 알림 목록이 보인다", async () => {
    show("chat-notifications");
    const dialog = await screen.findByRole("dialog", { name: "알림" });
    expect(await within(dialog).findByText("디자인 검토")).toBeInTheDocument();
  });

  it("sidebar-loading: 대화 목록이 불러오는 중으로 남는다", async () => {
    stubViewport(true);
    show("sidebar-loading");
    // 세션을 불러오는 화면에도 같은 문구가 있으므로 사이드바가 뜬 뒤에 그 안에서 찾는다.
    const nav = await screen.findByRole("navigation", { name: "대화 목록" });
    expect(within(nav).getByText("불러오는 중입니다.")).toBeInTheDocument();
  });

  it("sidebar-empty: 대화가 없다는 안내가 보인다", async () => {
    stubViewport(true);
    show("sidebar-empty");
    expect(await screen.findByText(/아직 대화가 없습니다/)).toBeInTheDocument();
  });

  it("sidebar-error: 오류 문구와 다시 불러오기 버튼이 보인다", async () => {
    stubViewport(true);
    show("sidebar-error");
    expect(await screen.findByText("대화 목록을 불러오지 못했습니다.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "다시 불러오기" })).toBeInTheDocument();
  });

  it("drawer-open: 넓은 화면에서는 일반 사이드바가 보인다", async () => {
    stubViewport(true);
    show("drawer-open");
    const nav = await screen.findByRole("navigation", { name: "대화 목록" });
    expect(
      await within(nav).findByRole("button", { name: /이번 주 일정 정리/ }),
    ).toBeInTheDocument();
  });

  it("drawer-open: 좁은 화면에서는 대화 목록 버튼을 눌러 드로어가 열린다", async () => {
    stubViewport(false);
    show("drawer-open");
    const dialog = await screen.findByRole("dialog", { name: "대화 목록" });
    expect(
      await within(dialog).findByRole("button", { name: /이번 주 일정 정리/ }),
    ).toBeInTheDocument();
  });

  it("connections-connected: 채팅 헤더에서 계정 연결 화면으로 가서 권한과 색인 수치가 보인다", async () => {
    show("connections-connected");
    expect(await screen.findByRole("heading", { name: "계정 연결" })).toBeInTheDocument();
    expect(await screen.findByText("preview@example.com")).toBeInTheDocument();
    expect(await screen.findByText("1,234통")).toBeInTheDocument();
    expect(screen.getByText("문서 권한: 허용됨")).toBeInTheDocument();
  });

  it("connections-indexing: 동기화 중 상태가 보인다", async () => {
    show("connections-indexing");
    expect(
      await screen.findByText("동기화하는 중입니다. 끝나면 이 화면이 저절로 바뀝니다."),
    ).toBeInTheDocument();
  });

  it("connections-none: 연결된 계정이 없다는 안내가 보인다", async () => {
    show("connections-none");
    expect(await screen.findByText(/연결된 계정이 없습니다/)).toBeInTheDocument();
  });

  it("connections-docs-off: 문서 권한 추가 버튼이 보인다", async () => {
    show("connections-docs-off");
    expect(await screen.findByRole("button", { name: "문서 권한 추가" })).toBeInTheDocument();
  });

  it("connections-confirm: 연결 해제 확인 상자가 열린다", async () => {
    show("connections-confirm");
    expect(await screen.findByRole("group", { name: "연결 해제 확인" })).toBeInTheDocument();
  });

  it("connections-error: 색인 정보를 불러오지 못했다는 오류가 보인다", async () => {
    show("connections-error");
    expect(
      await screen.findByText("색인 정보를 불러오지 못했습니다. 잠시 뒤 다시 시도해 주세요."),
    ).toBeInTheDocument();
  });

  it("login-signed-out: 로그인 버튼과 개발용 로그인 버튼이 보인다", async () => {
    show("login-signed-out");
    expect(await screen.findByRole("link", { name: "Google로 로그인" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "개발용 로그인" })).toBeInTheDocument();
  });

  it("login-error: 개발용 로그인 실패 문구가 보인다", async () => {
    show("login-error");
    expect(
      await screen.findByText(
        "로그인하지 못했습니다. 서버가 실행 중인지 확인한 뒤 다시 눌러 주세요.",
      ),
    ).toBeInTheDocument();
  });

  it("login-connection-error: 서버 연결 실패 문구와 다시 시도 버튼이 보인다", async () => {
    show("login-connection-error");
    expect(
      await screen.findByText("서버에 연결하지 못했습니다. 연결을 확인한 뒤 다시 시도해 주세요."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "다시 시도" })).toBeInTheDocument();
  });
});
