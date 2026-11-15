/** main과 preload가 같이 쓰는 IPC 채널 이름. 이 목록에 없는 채널은 열지 않는다. */
export const CHANNELS = {
  notify: "orbit-desktop:notify",
  setBadge: "orbit-desktop:set-badge",
  toggleVoice: "orbit-desktop:toggle-voice",
  startLogin: "orbit-desktop:start-login",
  loginEvent: "orbit-desktop:login-event",
  prepareConnect: "orbit-desktop:prepare-connect",
  connectEvent: "orbit-desktop:connect-event",
  openConnectFlow: "orbit-desktop:open-connect-flow",
  clearSession: "orbit-desktop:clear-session",
} as const;

/** IPC 인자 상한. main이 다시 검증한다(renderer는 신뢰하지 않는다). */
export const LIMITS = {
  idMax: 128,
  titleMax: 200,
  bodyMax: 500,
  badgeMax: 99_999,
} as const;

/** preload가 main에서 받는 앱 버전의 인자 이름(`webPreferences.additionalArguments`). */
export const VERSION_ARG = "--orbit-desktop-version=";
