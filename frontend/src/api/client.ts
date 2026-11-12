import type { z } from "zod";
import { ApiError, kindForStatus } from "./errors";

export const CSRF_COOKIE = "orbit_csrf";
export const CSRF_HEADER = "X-CSRF-Token";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface ApiClientOptions {
  fetch?: typeof fetch;
  /** 쿠키 값을 읽는 함수. 테스트에서 바꿔 끼운다. */
  getCookie?: (name: string) => string | undefined;
}

export interface RequestOptions<S extends z.ZodType | undefined> {
  body?: unknown;
  /** 응답 본문 검증 스키마. 없으면 본문을 읽지 않는다. */
  schema?: S;
  /** 요청에 더할 헤더(예: `Idempotency-Key`). CSRF·Accept·Content-Type은 덮어쓰지 못한다. */
  headers?: Record<string, string>;
}

export function readCookie(name: string): string | undefined {
  for (const part of document.cookie.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key !== name) continue;
    try {
      return decodeURIComponent(rest.join("="));
    } catch {
      return undefined; // 잘못된 퍼센트 인코딩이면 값이 없는 것으로 본다
    }
  }
  return undefined;
}

async function readDetail(response: Response): Promise<string | undefined> {
  try {
    const data: unknown = await response.json();
    if (typeof data === "object" && data !== null && "detail" in data) {
      const { detail } = data;
      if (typeof detail === "string") return detail;
    }
  } catch {
    // 본문이 JSON이 아니면 detail 없이 넘어간다.
  }
  return undefined;
}

export class ApiClient {
  private readonly fetchImpl: typeof fetch;
  private readonly getCookie: (name: string) => string | undefined;

  constructor(options: ApiClientOptions = {}) {
    this.fetchImpl = options.fetch ?? ((...args) => fetch(...args));
    this.getCookie = options.getCookie ?? readCookie;
  }

  async request<S extends z.ZodType | undefined = undefined>(
    method: Method,
    path: string,
    options: RequestOptions<S> = {},
  ): Promise<S extends z.ZodType ? z.output<S> : undefined> {
    const headers: Record<string, string> = { ...options.headers, Accept: "application/json" };
    if (!SAFE_METHODS.has(method)) {
      const token = this.getCookie(CSRF_COOKIE);
      if (token !== undefined) headers[CSRF_HEADER] = token;
    }
    const init: RequestInit = { method, headers, credentials: "same-origin" };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }

    let response: Response;
    try {
      response = await this.fetchImpl(path, init);
    } catch (cause) {
      throw new ApiError("network", { cause });
    }

    if (!response.ok) {
      const detail = await readDetail(response);
      throw new ApiError(kindForStatus(response.status), {
        status: response.status,
        ...(detail === undefined ? {} : { detail }),
      });
    }

    const schema = options.schema;
    if (schema === undefined) {
      return undefined as S extends z.ZodType ? z.output<S> : undefined;
    }
    let data: unknown;
    try {
      data = await response.json();
    } catch (cause) {
      throw new ApiError("invalid_response", { status: response.status, cause });
    }
    const parsed = schema.safeParse(data);
    if (!parsed.success) {
      throw new ApiError("invalid_response", { status: response.status, cause: parsed.error });
    }
    return parsed.data as S extends z.ZodType ? z.output<S> : undefined;
  }
}

export const api = new ApiClient();
