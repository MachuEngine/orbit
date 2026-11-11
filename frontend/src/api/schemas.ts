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

export const executionStatusSchema = z.enum([
  "pending",
  "succeeded",
  "failed",
  "unknown",
  "needs_user",
]);
export type ExecutionStatus = z.infer<typeof executionStatusSchema>;

/** `GET /api/session`. 응답 본문은 쓰지 않고 200 여부만 본다. 알 수 없는 필드는 허용한다. */
export const sessionSchema = z.looseObject({});
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
