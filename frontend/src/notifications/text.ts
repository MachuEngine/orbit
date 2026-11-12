/** 알림 화면에 쓰는 문구(ADR-0021). 서버가 주는 오류 `detail`은 화면에 옮기지 않는다. */
import { PRODUCT_NAME } from "../brand";

export const BELL_TITLE = "알림";

/** 헤더 알림 버튼의 접근 가능한 이름. 읽지 않은 알림 수를 함께 알린다. */
export function bellLabel(unread: number): string {
  return `알림 ${String(unread)}개`;
}

/** 배지에 보이는 숫자. 두 자리를 넘으면 줄여서 보인다. */
export function badgeText(unread: number): string {
  return unread > 99 ? "99+" : String(unread);
}

export const NOTIFY_TEXT = {
  panelTitle: "알림",
  close: "닫기",
  unreadAnnounce: (n: number) => (n > 0 ? `읽지 않은 알림 ${String(n)}개` : ""),

  arrivedHeading: "도착한 알림",
  arrivedEmpty: "도착한 알림이 없습니다.",
  arrivedLoading: "알림을 불러오는 중입니다.",
  arrivedError: "알림을 불러오지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
  unreadMark: "읽지 않음",
  markRead: "읽음",
  markReadLabel: (title: string) => `읽음으로 표시: ${title}`,
  markAllRead: "모두 읽음",
  markFailed: "읽음 표시를 하지 못했습니다. 잠시 뒤 다시 시도해 주세요.",

  upcomingHeading: "예정된 알림",
  upcomingEmpty: "예정된 알림이 없습니다.",
  upcomingLoading: "예정된 알림을 불러오는 중입니다.",
  upcomingError: "예정된 알림을 불러오지 못했습니다.",
  retry: "다시 불러오기",
  cancel: "취소",
  cancelLabel: (title: string) => `예정된 알림 취소: ${title}`,
  cancelConfirm: "이 알림을 취소할까요? 취소하면 알려 드리지 않습니다.",
  cancelYes: "알림 취소",
  cancelNo: "그대로 두기",
  cancelled: "예정된 알림을 취소했습니다.",
  cancelGone: "이미 도착했거나 취소된 알림입니다. 목록을 새로 불러왔습니다.",
  cancelFailed: "알림을 취소하지 못했습니다. 잠시 뒤 다시 시도해 주세요.",

  kindEvent: "일정",
  kindReminder: "리마인더",
  kindOther: "알림",
  eventStarts: (time: string) => `${time} 시작`,

  settingsHeading: "일정 알림 설정",
  settingsLoading: "설정을 불러오는 중입니다.",
  settingsError: "설정을 불러오지 못했습니다.",
  enabledLabel: "일정 알림 켜기",
  enabledHint: "연결한 계정의 기본 캘린더에서 곧 시작하는 일정을 알려 드립니다.",
  leadLabel: "알림 시점",
  leadOption: (minutes: number) => `일정 시작 ${String(minutes)}분 전`,
  settingsSaved: "설정을 저장했습니다.",
  settingsFailed: "설정을 저장하지 못했습니다. 잠시 뒤 다시 시도해 주세요.",

  browserHeading: "브라우저 알림",
  browserEnable: "브라우저 알림 켜기",
  browserHint: `${PRODUCT_NAME} 탭이 열려 있을 때 새 알림을 브라우저 알림으로도 보여 드립니다. 탭을 모두 닫으면 다음에 열 때 목록에서 확인할 수 있습니다.`,
  browserOn: `브라우저 알림이 켜져 있습니다. ${PRODUCT_NAME} 탭이 열려 있을 때만 알려 드립니다.`,
  browserDenied:
    "브라우저에서 알림이 차단되어 있습니다. 주소창 옆 사이트 설정에서 알림을 허용한 뒤 이 화면을 다시 열어 주세요.",
} as const;

export const LEAD_OPTIONS = [5, 10, 15, 30, 60] as const;

/** 브라우저 알림 본문. 제목은 외부 문자열이라 본문에 넣지 않고 종류만 알린다. */
export function browserBody(kind: string): string {
  return kind === "event_reminder" ? "곧 시작하는 일정입니다." : "리마인더입니다.";
}

export function kindText(kind: string): string {
  if (kind === "event_reminder") return NOTIFY_TEXT.kindEvent;
  if (kind === "reminder") return NOTIFY_TEXT.kindReminder;
  return NOTIFY_TEXT.kindOther;
}
