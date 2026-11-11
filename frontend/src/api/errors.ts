export type ApiErrorKind =
  | "unauthorized" // 401: 로그인이 필요하다
  | "csrf" // 403: CSRF 검사 실패
  | "not_found" // 404
  | "conflict" // 409: 승인 결정 경쟁·만료
  | "network" // 서버에 닿지 못했다
  | "server" // 5xx와 그 밖의 4xx
  | "invalid_response"; // 응답이 정의한 형태와 다르다

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;
  /** 서버가 준 `detail` 문자열. 409에서 `expired`인지 구분하는 데 쓴다. */
  readonly detail: string | null;

  constructor(kind: ApiErrorKind, options: { status?: number; detail?: string; cause?: unknown }) {
    super(`API 오류: ${kind}${options.status === undefined ? "" : ` (${options.status})`}`, {
      cause: options.cause,
    });
    this.name = "ApiError";
    this.kind = kind;
    this.status = options.status ?? null;
    this.detail = options.detail ?? null;
  }
}

export function kindForStatus(status: number): ApiErrorKind {
  if (status === 401) return "unauthorized";
  if (status === 403) return "csrf";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  return "server";
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}
