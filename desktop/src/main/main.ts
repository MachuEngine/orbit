import path from "node:path";
import {
  app,
  BrowserWindow,
  globalShortcut,
  ipcMain,
  Menu,
  nativeImage,
  Notification,
  powerMonitor,
  session,
  shell,
  systemPreferences,
  Tray,
  type IpcMainEvent,
  type MenuItemConstructorOptions,
} from "electron";
import { CHANNELS, VERSION_ARG } from "../shared/channels";
import { parseServerOrigin, parseShortcut } from "./config";
import { NotificationDedup } from "./dedup";
import { decideNavigation, decideWindowOpen, isServerUrl } from "./navigation";
import { isPermissionAllowed, micAccessAction } from "./permissions";
import { parseBadgeCount, parseNotifyPayload } from "./validate";

const PARTITION = "persist:orbit";

app.setName("Orbit");

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let quitting = false;
let registeredShortcut: string | null = null;
const dedup = new NotificationDedup();
// 알림 객체는 참조를 잡아 두지 않으면 클릭 전에 수거될 수 있다.
const liveNotifications = new Set<Notification>();

function openExternal(url: string): void {
  void shell.openExternal(url).catch(() => {
    console.warn("[orbit-desktop] 시스템 브라우저를 열지 못했습니다.");
  });
}

function showWindow(): void {
  const win = mainWindow;
  if (win === null || win.isDestroyed()) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
  if (process.platform === "darwin") app.focus({ steal: true });
}

/** 오디오 `media` 요청 때 macOS 마이크 접근 상태를 확인하고 필요하면 시스템에 묻는다. */
async function ensureMicrophoneAccess(): Promise<boolean> {
  const action = micAccessAction(
    process.platform,
    process.platform === "darwin" ? systemPreferences.getMediaAccessStatus("microphone") : "",
  );
  if (action === "allow") return true;
  if (action === "deny") return false;
  return systemPreferences.askForMediaAccess("microphone");
}

function configureSession(origin: string): void {
  const ses = session.fromPartition(PARTITION);
  ses.setPermissionRequestHandler((_contents, permission, callback, details) => {
    const allowed = isPermissionAllowed(
      {
        permission,
        requestingUrl: details.requestingUrl,
        mediaTypes: "mediaTypes" in details ? details.mediaTypes : undefined,
      },
      origin,
    );
    if (!allowed) {
      callback(false);
      return;
    }
    if (permission === "media") {
      void ensureMicrophoneAccess().then(callback, () => {
        callback(false);
      });
      return;
    }
    callback(true);
  });
  ses.setPermissionCheckHandler((_contents, permission, requestingOrigin, details) =>
    isPermissionAllowed(
      {
        permission,
        requestingUrl: details.requestingUrl ?? requestingOrigin,
        mediaTypes: details.mediaType === undefined ? undefined : [details.mediaType],
      },
      origin,
    ),
  );
}

/** 모든 webContents에 이동·새 창 제한을 건다(ADR-0024 §3). */
function installNavigationGuards(origin: string): void {
  app.on("web-contents-created", (_event, contents) => {
    contents.setWindowOpenHandler(({ url }) => {
      const { external } = decideWindowOpen(url);
      if (external !== null) openExternal(external);
      return { action: "deny" };
    });
    const guard = (event: { url: string; preventDefault: () => void }) => {
      const decision = decideNavigation(event.url, origin);
      if (decision.action === "allow") return;
      event.preventDefault();
      if (decision.action === "external") openExternal(decision.url);
    };
    contents.on("will-navigate", guard);
    contents.on("will-redirect", guard);
    // 하위 프레임(iframe)도 서버 출처가 아니면 막는다. 시스템 브라우저로 넘기지는 않는다.
    contents.on("will-frame-navigate", (event) => {
      if (event.isMainFrame) return;
      if (decideNavigation(event.url, origin).action !== "allow") event.preventDefault();
    });
    contents.on("will-attach-webview", (event) => {
      event.preventDefault();
    });
  });
}

/** IPC 발신자가 앱 창의 최상위 프레임이고 서버 출처인지 확인한다. */
function isTrustedSender(event: IpcMainEvent, origin: string): boolean {
  const frame = event.senderFrame;
  const win = mainWindow;
  if (frame === null || win === null || win.isDestroyed()) return false;
  if (event.sender !== win.webContents) return false;
  return frame.parent === null && isServerUrl(frame.url, origin);
}

function registerIpc(origin: string): void {
  ipcMain.on(CHANNELS.notify, (event, raw: unknown) => {
    if (!isTrustedSender(event, origin)) return;
    const payload = parseNotifyPayload(raw);
    if (payload === null || !Notification.isSupported()) return;
    if (!dedup.shouldShow(payload.id)) return;
    const notification = new Notification({ title: payload.title, body: payload.body });
    liveNotifications.add(notification);
    const release = () => {
      liveNotifications.delete(notification);
    };
    notification.on("click", () => {
      showWindow();
      release();
    });
    notification.on("close", release);
    notification.on("failed", release);
    notification.show();
  });

  ipcMain.on(CHANNELS.setBadge, (event, raw: unknown) => {
    if (!isTrustedSender(event, origin)) return;
    const count = parseBadgeCount(raw);
    if (count === null) return;
    app.setBadgeCount(count);
  });
}

function createWindow(origin: string): BrowserWindow {
  const win = new BrowserWindow({
    width: 1100,
    height: 800,
    minWidth: 480,
    minHeight: 480,
    title: "Orbit",
    webPreferences: {
      partition: PARTITION,
      preload: path.join(__dirname, "preload.cjs"),
      additionalArguments: [`${VERSION_ARG}${app.getVersion()}`],
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      // 창이 숨겨져도 30초 알림 조회(ADR-0021)가 멈추지 않게 한다.
      backgroundThrottling: false,
    },
  });
  // 닫으면 숨기고 상주한다. 실제 종료는 Cmd+Q와 트레이 메뉴의 종료뿐이다.
  win.on("close", (event) => {
    if (quitting) return;
    event.preventDefault();
    win.hide();
  });
  win.on("closed", () => {
    mainWindow = null;
  });
  void win.loadURL(`${origin}/`);
  return win;
}

function createTray(): void {
  const image = nativeImage.createFromPath(
    path.join(__dirname, "..", "assets", "iconTemplate.png"),
  );
  image.setTemplateImage(true);
  tray = new Tray(image);
  tray.setToolTip("Orbit");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "Orbit 열기", click: showWindow },
      { type: "separator" },
      {
        label: "종료",
        click: () => {
          app.quit();
        },
      },
    ]),
  );
}

function createMenu(): void {
  const view: MenuItemConstructorOptions[] = [{ role: "reload" }];
  if (!app.isPackaged) view.push({ role: "toggleDevTools" });
  Menu.setApplicationMenu(
    Menu.buildFromTemplate([
      { role: "appMenu" },
      { role: "editMenu" },
      { label: "보기", submenu: view },
      { role: "windowMenu" },
    ]),
  );
}

function registerShortcut(accelerator: string | null): void {
  if (accelerator === null) return;
  try {
    const ok = globalShortcut.register(accelerator, () => {
      showWindow();
      const win = mainWindow;
      if (win !== null && !win.isDestroyed()) win.webContents.send(CHANNELS.toggleVoice);
    });
    if (ok) registeredShortcut = accelerator;
    else
      console.warn(
        `[orbit-desktop] 전역 단축키 ${accelerator}를 등록하지 못했습니다(다른 앱이 사용 중일 수 있습니다).`,
      );
  } catch {
    console.warn(`[orbit-desktop] 전역 단축키 ${accelerator}가 올바른 형식이 아닙니다.`);
  }
}

function main(): void {
  const origin = parseServerOrigin(process.env.ORBIT_DESKTOP_URL);
  const shortcut = parseShortcut(process.env.ORBIT_DESKTOP_SHORTCUT);

  if (!app.requestSingleInstanceLock()) {
    app.quit();
    return;
  }
  app.on("second-instance", showWindow);
  app.on("before-quit", () => {
    quitting = true;
  });
  app.on("will-quit", () => {
    if (registeredShortcut !== null) globalShortcut.unregister(registeredShortcut);
    registeredShortcut = null;
  });
  // 창을 숨겨 두는 동안에도 앱은 계속 실행한다.
  app.on("window-all-closed", () => undefined);
  app.on("activate", showWindow);

  installNavigationGuards(origin);

  void app.whenReady().then(() => {
    // 로그아웃·재시작·종료 때 창 닫기 취소(숨기기)가 시스템 종료를 막지 않게 한다.
    powerMonitor.on("shutdown", () => {
      quitting = true;
    });
    configureSession(origin);
    registerIpc(origin);
    createMenu();
    createTray();
    mainWindow = createWindow(origin);
    registerShortcut(shortcut);
  });
}

main();
