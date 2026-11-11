// 응답 형태는 backend/src/orbit/api/runs.py 기준이다. 정의하지 않은 값은 거부한다.
import { z } from "zod";

export const runStatusSchema = z.enum([
  "queued",
  "running",
  "waiting_approval",
  "succeeded",
  "failed",
  "cancelled",
]);
export type RunStatus = z.infer<typeof runStatusSchema>;

export const TERMINAL_RUN_STATUSES: readonly RunStatus[] = ["succeeded", "failed", "cancelled"];

export function isTerminalStatus(status: RunStatus): boolean {
  return TERMINAL_RUN_STATUSES.includes(status);
}

export const provenanceSchema = z.enum(["user", "workspace_data", "external_content", "model"]);
export type Provenance = z.infer<typeof provenanceSchema>;

export const approvalStatusSchema = z.enum(["pending", "approved", "rejected", "expired"]);
export type ApprovalStatus = z.infer<typeof approvalStatusSchema>;

// 백엔드는 승인이 거절·만료된 경우에도 승인 상태를 그대로 execution_result에 담는다(nodes.py).
export const executionStatusSchema = z.enum([
  "rejected",
  "expired",
  "pending",
  "succeeded",
  "failed",
  "unknown",
  "needs_user",
]);
export type ExecutionStatus = z.infer<typeof executionStatusSchema>;

/**
 * `GET /api/session`. 로그인하지 않았어도 200이고 `authenticated`가 false다.
 * `display_name`·`email`은 이 세션의 사용자 본인 정보이고, 개발용 세션에는 없다(null).
 * `dev_login`·`google_login`은 서버가 열어 둔 로그인 수단이다.
 */
export const sessionSchema = z.object({
  authenticated: z.boolean(),
  workspace_id: z.string().nullable(),
  expires_at: z.string().nullable(),
  display_name: z.string().nullable(),
  email: z.string().nullable(),
  dev_login: z.boolean(),
  google_login: z.boolean(),
});
export type Session = z.infer<typeof sessionSchema>;

/** `POST /api/runs` */
export const createRunResponseSchema = z.object({
  run_id: z.string(),
  conversation_id: z.string(),
  status: runStatusSchema,
});
export type CreateRunResponse = z.infer<typeof createRunResponseSchema>;

/** `GET /api/runs/{id}` */
export const runSchema = z.object({
  id: z.string(),
  conversation_id: z.string(),
  status: runStatusSchema,
  error_kind: z.string().nullable(),
  pending_approval_id: z.string().nullable(),
  created_at: z.string(),
  updated_at: z.string(),
});
export type Run = z.infer<typeof runSchema>;

/** `GET /api/approvals/{id}` */
export const approvalSchema = z.object({
  id: z.string(),
  run_id: z.string(),
  status: approvalStatusSchema,
  tool_name: z.string(),
  args: z.record(z.string(), z.unknown()),
  summary: z.record(z.string(), z.unknown()),
  /** 이메일(소문자) -> 출처 */
  provenance: z.record(z.string(), provenanceSchema),
  /** 대상 요약. 일정 변경·취소는 현재 제목·시각, 메일 답장은 스레드 제목. 없으면 null */
  target: z
    .union([
      z.object({ kind: z.literal("event"), title: z.string(), start: z.string(), end: z.string() }),
      z.object({ kind: z.literal("thread"), subject: z.string() }),
    ])
    .nullable()
    .default(null),
  expires_at: z.string(),
  decided_at: z.string().nullable(),
});
export type Approval = z.infer<typeof approvalSchema>;

export type Decision = "approve" | "reject";

/** `POST /api/approvals/{id}/decision` */
export const decisionResponseSchema = z.object({
  status: z.enum(["approved", "rejected"]),
});
export type DecisionResponse = z.infer<typeof decisionResponseSchema>;

/** `GET /api/connections`. 토큰 관련 값은 응답에 없다. `mode`가 google일 때만 연결이 필요하다. */
export const connectionSchema = z.object({
  id: z.string(),
  provider: z.string(),
  status: z.enum(["pending", "active", "revoked", "error"]),
  account_email: z.string(),
  scopes: z.array(z.string()),
  created_at: z.string(),
});
export type Connection = z.infer<typeof connectionSchema>;

export const connectionsResponseSchema = z.object({
  mode: z.enum(["sim", "google"]),
  connect_available: z.boolean(),
  connections: z.array(connectionSchema),
});
export type ConnectionsResponse = z.infer<typeof connectionsResponseSchema>;

/** `POST /api/connections/google/start`. 화면이 이 주소로 직접 이동한다. */
export const startConnectionResponseSchema = z.object({ authorization_url: z.string() });

/** `DELETE /api/connections/{id}`. `provider_revoke`는 Google 쪽 철회 결과다. */
export const disconnectResponseSchema = z.object({
  id: z.string(),
  status: z.literal("revoked"),
  provider_revoke: z.enum(["revoked", "failed", "none"]),
});
export type DisconnectResponse = z.infer<typeof disconnectResponseSchema>;
