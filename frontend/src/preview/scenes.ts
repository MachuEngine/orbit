// 개발 전용 화면 미리보기의 장면 정의. 백엔드 없이 앱 화면을 띄우는 데 필요한 합성 데이터를 한 파일에 모은다.
// 모든 값은 합성이다(주소는 example.com 예약 도메인). 응답 모양은 api/schemas.ts의 스키마를 따른다.
// 다음 단계에서 `connections`·대화 목록 같은 장면을 더할 때는 SCENES에 항목만 추가한다.
import type {
  Approval,
  ConnectionsResponse,
  ConversationDetail,
  ConversationSummary,
  IndexStatus,
  MailLabel,
  NotificationItem,
  NotificationSettings,
  Session,
} from "../api/schemas";

export type SceneId =
  | "chat-empty"
  | "chat-conversation"
  | "chat-approval"
  | "chat-needs-connection"
  | "chat-working"
  | "chat-notifications"
  | "sidebar-loading"
  | "sidebar-empty"
  | "sidebar-error"
  | "drawer-open"
  | "connections-connected"
  | "connections-indexing"
  | "connections-none"
  | "connections-docs-off"
  | "connections-confirm"
  | "connections-error"
  | "login-signed-out"
  | "login-error"
  | "login-connection-error";

/** 계정 연결 장면의 데이터 종류. 연결이 없는 장면은 `needsConnection`을 쓴다. */
export type ConnectionsVariant = "connected" | "indexing" | "docs-off" | "confirm" | "error";

/** 로그인 화면 장면의 종류. 세션이 없는 상태(signed-out), 개발용 로그인 실패(dev-failed), 세션 확인 실패(connection-error). */
export type LoginVariant = "signed-out" | "dev-failed" | "connection-error";

export interface Scene {
  id: SceneId;
  /** 장면 선택 링크에 보이는 이름 */
  label: string;
  /** 화면이 뜬 뒤 대신 눌러 줄 동작. 앱 코드는 바꾸지 않고 화면의 버튼을 누르는 것으로 장면을 만든다. */
  open?: {
    conversation?: string;
    notifications?: boolean;
    drawer?: boolean;
    /** 채팅 헤더의 "계정 연결" 버튼을 눌러 계정 연결 화면으로 간다. */
    connections?: boolean;
    /** 계정 연결 화면에서 "연결 해제" 버튼을 눌러 확인 상자를 연다. */
    confirmDisconnect?: boolean;
    /** 로그인 화면의 "개발용 로그인" 버튼을 눌러 로그인 실패 문구를 띄운다. */
    devLogin?: boolean;
  };
  /** 로그인 화면 장면. 있으면 세션이 로그아웃 상태이거나(또는 세션 확인이 실패한다). */
  login?: LoginVariant;
  /** 계정 연결 화면이 쓰는 데이터 종류. */
  connections?: ConnectionsVariant;
  /** Google 연결이 없는 상태(연결 필요 배너) */
  needsConnection?: boolean;
  /** 대화 목록 응답 방식. pending은 영구 대기(불러오는 중), empty는 대화 0개, error는 500이다. */
  list?: "pending" | "empty" | "error";
}

const EMPTY_SCENE: Scene = { id: "chat-empty", label: "채팅: 빈 화면" };

export const SCENES: readonly Scene[] = [
  EMPTY_SCENE,
  { id: "chat-conversation", label: "채팅: 대화", open: { conversation: "이번 주 일정" } },
  { id: "chat-approval", label: "채팅: 승인 대기", open: { conversation: "회의 자료 답장" } },
  { id: "chat-needs-connection", label: "채팅: 연결 필요", needsConnection: true },
  { id: "chat-working", label: "채팅: 응답 작성 중", open: { conversation: "받은 메일 요약" } },
  { id: "chat-notifications", label: "채팅: 알림 패널", open: { notifications: true } },
  { id: "sidebar-loading", label: "사이드바: 불러오는 중", list: "pending" },
  { id: "sidebar-empty", label: "사이드바: 대화 없음", list: "empty" },
  { id: "sidebar-error", label: "사이드바: 불러오기 실패", list: "error" },
  { id: "drawer-open", label: "사이드바: 드로어 열림(좁은 화면)", open: { drawer: true } },
  {
    id: "connections-connected",
    label: "계정 연결: 연결됨",
    connections: "connected",
    open: { connections: true },
  },
  {
    id: "connections-indexing",
    label: "계정 연결: 색인 동기화 중",
    connections: "indexing",
    open: { connections: true },
  },
  {
    id: "connections-none",
    label: "계정 연결: 연결 없음",
    needsConnection: true,
    open: { connections: true },
  },
  {
    id: "connections-docs-off",
    label: "계정 연결: 문서 권한 없음",
    connections: "docs-off",
    open: { connections: true },
  },
  {
    id: "connections-confirm",
    label: "계정 연결: 해제 확인",
    connections: "confirm",
    open: { connections: true, confirmDisconnect: true },
  },
  {
    id: "connections-error",
    label: "계정 연결: 색인 정보 오류",
    connections: "error",
    open: { connections: true },
  },
  { id: "login-signed-out", label: "로그인: 로그아웃 상태", login: "signed-out" },
  {
    id: "login-error",
    label: "로그인: 로그인 실패 문구",
    login: "dev-failed",
    open: { devLogin: true },
  },
  { id: "login-connection-error", label: "로그인: 서버 연결 실패", login: "connection-error" },
];

/** 주소의 scene 값에 맞는 장면. 없거나 모르는 값이면 빈 화면이다. */
export function findScene(id: string | null): Scene {
  return SCENES.find((scene) => scene.id === id) ?? EMPTY_SCENE;
}

/** 실시간 이벤트(SSE) 하나. 모양은 runs/events.ts가 검증한다. */
export interface FakeRunEvent {
  type: string;
  payload: Record<string, unknown>;
}

/** 한 장면이 쓰는 서버 쪽 데이터 전체. 시각은 `now` 기준 상대값이라 "3분 전"이 늘 맞게 보인다. */
export interface SceneData {
  session: Session;
  connections: ConnectionsResponse;
  conversations: ConversationSummary[];
  details: Record<string, ConversationDetail>;
  approvals: Record<string, Approval>;
  runEvents: Record<string, FakeRunEvent[]>;
  notifications: NotificationItem[];
  upcoming: NotificationItem[];
  notificationSettings: NotificationSettings;
  /** 연결 id별 메일 색인 상태 */
  indexes: Record<string, IndexStatus>;
  /** 색인 범위로 고를 수 있는 메일 라벨 */
  labels: MailLabel[];
  /** 색인 상태 요청이 500으로 실패하는 장면 */
  indexError?: boolean;
  /** 대화 목록 요청에 대한 응답 방식(장면의 `list`). 없으면 정상 응답이다. */
  listMode?: "pending" | "error";
  /** 세션 확인 요청이 500으로 실패하는 장면(서버에 닿지 못한 상태) */
  sessionError?: boolean;
  /** 개발용 로그인 요청이 500으로 실패하는 장면 */
  devLoginFails?: boolean;
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** 새 메시지를 보냈을 때 미리보기가 돌려주는 응답. 실제 응답은 만들지 않는다. */
export const PREVIEW_REPLY = "미리보기 화면입니다. 실제 응답은 만들지 않습니다.";

export function buildSceneData(scene: Scene, now: number): SceneData {
  const at = (offset: number) => new Date(now + offset).toISOString();

  const signedOut = scene.login !== undefined;
  const session: Session = signedOut
    ? {
        authenticated: false,
        workspace_id: null,
        expires_at: null,
        display_name: null,
        email: null,
        dev_login: true,
        google_login: true,
      }
    : {
        authenticated: true,
        workspace_id: "ws-preview",
        expires_at: at(8 * HOUR),
        display_name: "미리보기 사용자",
        email: "preview@example.com",
        dev_login: false,
        google_login: true,
      };

  const variant = scene.connections;
  const scopePrefix = "preview/auth/";
  const connections: ConnectionsResponse =
    variant !== undefined
      ? {
          mode: "google",
          connect_available: true,
          connections: [
            {
              id: "conn-preview",
              provider: "google",
              status: "active",
              account_email: "preview@example.com",
              scopes: [
                "calendar.events",
                "calendar.calendarlist.readonly",
                "gmail.readonly",
                "gmail.compose",
                ...(variant === "docs-off" ? [] : ["drive.readonly"]),
              ].map((name) => `${scopePrefix}${name}`),
              docs_granted: variant !== "docs-off",
              created_at: at(-12 * DAY),
            },
          ],
        }
      : scene.needsConnection
        ? { mode: "google", connect_available: true, connections: [] }
        : { mode: "sim", connect_available: false, connections: [] };

  const indexes: Record<string, IndexStatus> = {};
  if (variant === "indexing") {
    // 범위 하나는 동기화를 마쳤고 하나는 아직이다. 서버가 동기화를 대기열에 넣은 상태다.
    indexes["conn-preview"] = {
      scopes: [
        { id: "sc-1", label_id: "INBOX", label_name: "INBOX", window_days: 90, synced: true },
        { id: "sc-2", label_id: "Label_1", label_name: "프로젝트", window_days: 30, synced: false },
      ],
      indexed_mail_count: 842,
      last_synced_at: at(-2 * HOUR),
      status: "pending",
      error_kind: null,
      sync_queued: true,
    };
  } else if (variant !== undefined) {
    indexes["conn-preview"] = {
      scopes: [
        { id: "sc-1", label_id: "INBOX", label_name: "INBOX", window_days: 90, synced: true },
        { id: "sc-2", label_id: "Label_1", label_name: "프로젝트", window_days: 30, synced: true },
      ],
      indexed_mail_count: 1234,
      last_synced_at: at(-12 * MINUTE),
      status: "ok",
      error_kind: null,
      sync_queued: false,
    };
  }
  const labels: MailLabel[] = [
    { id: "INBOX", name: "INBOX", kind: "system" },
    { id: "SENT", name: "SENT", kind: "system" },
    { id: "STARRED", name: "STARRED", kind: "system" },
    { id: "Label_1", name: "프로젝트", kind: "user" },
    { id: "Label_2", name: "청구서", kind: "user" },
  ];

  const conversations: ConversationSummary[] =
    scene.list === "empty"
      ? []
      : [
          {
            id: "conv-schedule",
            title: "이번 주 일정 정리",
            created_at: at(-3 * HOUR),
            last_activity_at: at(-3 * MINUTE),
            last_run_status: "succeeded",
          },
          {
            id: "conv-mail",
            title: "회의 자료 답장 보내기",
            created_at: at(-5 * HOUR),
            last_activity_at: at(-40 * MINUTE),
            last_run_status: "waiting_approval",
          },
          {
            id: "conv-working",
            title: "받은 메일 요약",
            created_at: at(-1 * HOUR),
            last_activity_at: at(-90_000),
            last_run_status: "running",
          },
          {
            id: "conv-lunch",
            title: "점심 약속 일정 만들기",
            created_at: at(-1 * DAY - 2 * HOUR),
            last_activity_at: at(-1 * DAY),
            last_run_status: "succeeded",
          },
        ];

  const details: Record<string, ConversationDetail> = {
    "conv-schedule": {
      id: "conv-schedule",
      turns: [
        {
          run_id: "run-schedule-1",
          status: "succeeded",
          user_text: "이번 주 일정을 정리해 줘",
          assistant_text: [
            "## 이번 주 일정",
            "",
            "- 월요일 `09:30` 주간 회의",
            "- 수요일 `14:00` 디자인 검토",
            "- 금요일 `16:00` 회고",
            "",
            "**수요일 검토**는 자료를 미리 읽어 두면 좋습니다.",
          ].join("\n"),
          pending_approval_id: null,
          created_at: at(-3 * HOUR),
        },
        {
          run_id: "run-schedule-2",
          status: "running",
          user_text: "수요일 검토 시간을 한 시간 뒤로 옮기고 자료 위치도 알려 줘",
          assistant_text: null,
          pending_approval_id: null,
          created_at: at(-3 * MINUTE),
        },
      ],
    },
    "conv-mail": {
      id: "conv-mail",
      turns: [
        {
          run_id: "run-mail-1",
          status: "waiting_approval",
          user_text: "회의 자료를 요청한 메일에 답장을 보내 줘",
          assistant_text: null,
          pending_approval_id: "ap-preview-mail",
          created_at: at(-40 * MINUTE),
        },
      ],
    },
    "conv-working": {
      id: "conv-working",
      turns: [
        {
          run_id: "run-working-1",
          status: "running",
          user_text: "오늘 받은 메일을 요약해 줘",
          assistant_text: null,
          pending_approval_id: null,
          created_at: at(-90_000),
        },
      ],
    },
    "conv-lunch": {
      id: "conv-lunch",
      turns: [
        {
          run_id: "run-lunch-1",
          status: "succeeded",
          user_text: "내일 점심 약속을 일정으로 만들어 줘",
          assistant_text: "내일 12:00에 점심 약속 일정을 만들었습니다.",
          pending_approval_id: null,
          created_at: at(-1 * DAY - 2 * HOUR),
        },
      ],
    },
  };

  // 끝나지 않은 마지막 턴은 화면이 그 run의 이벤트를 처음부터 다시 받아 채운다(conversations/turns.ts).
  const runEvents: Record<string, FakeRunEvent[]> = {
    "run-schedule-2": [
      { type: "status", payload: { status: "running" } },
      {
        type: "approval_requested",
        payload: {
          step: 1,
          approval_id: "ap-preview-event",
          tool_name: "update_event_time",
          summary: {},
        },
      },
      {
        type: "execution_result",
        payload: { step: 1, approval_id: "ap-preview-event", status: "succeeded" },
      },
      {
        type: "assistant_text",
        payload: {
          step: 2,
          text: [
            "수요일 디자인 검토를 `15:00`으로 옮겼습니다.",
            "",
            "### 자료 위치",
            "",
            "1. 공유 폴더의 [검토 자료](docs.example.com/design-review)",
            "2. 지난 회의 메모",
            "",
            "참석자에게는 **변경 알림**이 따로 가지 않으니 필요하면 알려 주세요.",
          ].join("\n"),
        },
      },
      { type: "status", payload: { status: "succeeded" } },
    ],
    "run-mail-1": [
      { type: "status", payload: { status: "running" } },
      {
        type: "assistant_text",
        payload: {
          step: 1,
          text: "답장 초안을 만들었습니다. 아래 카드에서 내용을 확인해 주세요.",
        },
      },
      {
        type: "approval_requested",
        payload: {
          step: 2,
          approval_id: "ap-preview-mail",
          tool_name: "send_mail",
          summary: { to_count: 2 },
        },
      },
      { type: "status", payload: { status: "waiting_approval" } },
    ],
    // 끝나지 않는 run: 응답을 쓰는 중으로 보인다.
    "run-working-1": [{ type: "status", payload: { status: "running" } }],
  };

  const approvals: Record<string, Approval> = {
    "ap-preview-mail": {
      id: "ap-preview-mail",
      run_id: "run-mail-1",
      status: "pending",
      tool_name: "send_mail",
      args: {
        to: ["kim@example.com", "partner@example.com"],
        subject: "회의 자료 공유 부탁드립니다",
        body: [
          "안녕하세요.",
          "요청하신 회의 자료를 정리해 보냅니다.",
          "",
          "- 일정 요약",
          "- 논의가 필요한 항목",
          "",
          "확인 후 의견 주세요. 감사합니다.",
        ].join("\n"),
      },
      summary: { to_count: 2 },
      provenance: { "kim@example.com": "user", "partner@example.com": "external_content" },
      target: null,
      expires_at: at(9 * MINUTE + 30_000),
      decided_at: null,
    },
  };

  const notifications: NotificationItem[] = [
    {
      id: "n-1",
      kind: "event_reminder",
      title: "디자인 검토",
      due_at: at(-5 * MINUTE),
      event_start: at(25 * MINUTE),
      read: false,
      created_at: at(-1 * HOUR),
    },
    {
      id: "n-2",
      kind: "reminder",
      title: "회의 자료 답장 확인",
      due_at: at(-2 * HOUR),
      event_start: null,
      read: false,
      created_at: at(-1 * DAY),
    },
    {
      id: "n-3",
      kind: "event_reminder",
      title: "주간 회의",
      due_at: at(-1 * DAY),
      event_start: at(-1 * DAY + 10 * MINUTE),
      read: true,
      created_at: at(-2 * DAY),
    },
  ];

  const upcoming: NotificationItem[] = [
    {
      id: "u-1",
      kind: "event_reminder",
      title: "팀 점심",
      due_at: at(3 * HOUR),
      event_start: at(3 * HOUR + 30 * MINUTE),
      read: false,
      created_at: at(-1 * HOUR),
    },
  ];

  return {
    session,
    connections,
    conversations,
    details,
    approvals,
    runEvents,
    notifications,
    upcoming,
    notificationSettings: { event_reminders_enabled: true, lead_minutes: 10 },
    indexes,
    labels,
    indexError: variant === "error" ? true : undefined,
    sessionError: scene.login === "connection-error" ? true : undefined,
    devLoginFails: scene.login === "dev-failed" ? true : undefined,
    listMode: scene.list === "pending" || scene.list === "error" ? scene.list : undefined,
  };
}
