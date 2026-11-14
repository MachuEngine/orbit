// 장면이 정한 동작을 화면의 버튼을 눌러 대신 한다. 앱 코드는 장면을 모르므로 역할·이름으로 버튼을 찾는다.
import type { Scene } from "./scenes";

const GIVE_UP_MS = 5000;
const POLL_MS = 50;

function buttons(root: ParentNode): HTMLButtonElement[] {
  return Array.from(root.querySelectorAll<HTMLButtonElement>("button"));
}

/** 대화 목록에서 제목이 맞는 대화를 연다. 좁은 화면이면 먼저 "대화 목록" 버튼으로 대화 상자를 연다(`onStep`을 부른다). */
function openConversation(title: string, onStep: () => void): boolean {
  const list = document.querySelector('[aria-label="대화 목록"]');
  const item =
    list === null
      ? undefined
      : buttons(list).find((button) => button.textContent.includes(title) && !button.disabled);
  if (item !== undefined) {
    item.click();
    return true;
  }
  if (list === null) {
    const toggle = buttons(document).find((button) => button.textContent.trim() === "대화 목록");
    if (toggle !== undefined) {
      toggle.click();
      onStep();
    }
  }
  return false;
}

/** 좁은 화면이면 "대화 목록" 버튼을 눌러 드로어를 연다. 넓은 화면은 일반 사이드바가 이미 보이므로 끝난 것으로 본다. */
function openDrawer(onStep: () => void): boolean {
  if (document.querySelector('[aria-label="대화 목록"]') !== null) return true;
  const toggle = buttons(document).find((button) => button.textContent.trim() === "대화 목록");
  if (toggle !== undefined) {
    toggle.click();
    onStep();
  }
  return false;
}

function openNotifications(): boolean {
  const bell = buttons(document).find((button) =>
    (button.getAttribute("aria-label") ?? "").startsWith("알림"),
  );
  if (bell === undefined) return false;
  bell.click();
  return true;
}

/** 채팅 헤더의 "계정 연결" 버튼으로 계정 연결 화면에 간다. 이미 화면에 있으면 끝난 것이다. */
function openConnections(): boolean {
  if (document.querySelector("main.conn") !== null) return true;
  const header = document.querySelector(".chat-header");
  const button =
    header === null
      ? undefined
      : buttons(header).find((item) => item.textContent.trim() === "계정 연결");
  button?.click();
  return false;
}

/** 연결 카드의 "연결 해제" 버튼을 눌러 확인 상자를 연다. 카드가 그려질 때까지 기다린다. */
function openDisconnectConfirm(): boolean {
  if (document.querySelector('[aria-label="연결 해제 확인"]') !== null) return true;
  const card = document.querySelector(".conn-card");
  const button =
    card === null
      ? undefined
      : buttons(card).find((item) => item.textContent.trim() === "연결 해제" && !item.disabled);
  button?.click();
  return false;
}

/** 로그인 화면의 "개발용 로그인" 버튼을 누른다. 화면이 그려질 때까지 기다린다. */
function clickDevLogin(): boolean {
  const button = buttons(document).find(
    (item) => item.textContent.trim() === "개발용 로그인" && !item.disabled,
  );
  if (button === undefined) return false;
  button.click();
  return true;
}

/** 목록이나 버튼이 그려질 때까지 기다렸다가 한 번 누른다. 5초 안에 못 찾으면 포기한다. */
export function runSceneActions(scene: Scene): void {
  const { open } = scene;
  if (open === undefined) return;
  let deadline = Date.now() + GIVE_UP_MS;
  const tick = () => {
    const done =
      (open.conversation === undefined ||
        openConversation(open.conversation, () => {
          deadline = Date.now() + GIVE_UP_MS; // 대화 상자를 열었으면 목록을 기다릴 시간을 다시 준다
        })) &&
      (open.notifications !== true || openNotifications()) &&
      (open.connections !== true || openConnections()) &&
      (open.confirmDisconnect !== true || openDisconnectConfirm()) &&
      (open.devLogin !== true || clickDevLogin()) &&
      (open.drawer !== true ||
        openDrawer(() => {
          deadline = Date.now() + GIVE_UP_MS;
        }));
    if (!done && Date.now() < deadline) window.setTimeout(tick, POLL_MS);
  };
  tick();
}
