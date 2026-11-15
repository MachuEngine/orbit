import { isServerUrl } from "./navigation";

/** Electron 권한 요청에서 판정에 쓰는 값만 뽑은 것. */
export interface PermissionRequest {
  permission: string;
  /** 요청한 페이지의 주소(요청 핸들러는 `requestingUrl`, 확인 핸들러는 `requestingOrigin`). */
  requestingUrl: string;
  /** 요청 핸들러의 `details.mediaTypes`. 확인 핸들러는 `[mediaType]`로 채운다. */
  mediaTypes?: readonly string[] | undefined;
}

/**
 * 서버 출처의 `media`(오디오만)와 `notifications`만 허용한다(ADR-0024 §3). 비디오가 섞인 요청과
 * 종류를 알 수 없는 미디어 요청은 거부한다.
 */
export function isPermissionAllowed(request: PermissionRequest, serverOrigin: string): boolean {
  if (!isServerUrl(request.requestingUrl, serverOrigin)) return false;
  if (request.permission === "notifications") return true;
  if (request.permission === "media") {
    const types = request.mediaTypes ?? [];
    return types.length > 0 && types.every((type) => type === "audio");
  }
  return false;
}

export type MicAction = "allow" | "ask" | "deny";

/**
 * macOS 마이크 접근 상태에 따른 행동. macOS가 아니면 그대로 허용한다(OS 확인 대상이 아니다).
 * `not-determined`면 시스템에 묻고, `denied`·`restricted`면 거부한다. 알 수 없는 상태는 묻는다.
 */
export function micAccessAction(platform: string, status: string): MicAction {
  if (platform !== "darwin") return "allow";
  if (status === "granted") return "allow";
  if (status === "denied" || status === "restricted") return "deny";
  return "ask";
}
