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

/** `GET /api/conversations`. 최근 활동 순이다. 제목은 첫 사용자 메시지에서 만들며 없을 수 있다. */
export const conversationSummarySchema = z.object({
  id: z.string(),
  title: z.string().nullable(),
  created_at: z.string(),
  last_activity_at: z.string(),
  last_run_status: runStatusSchema.nullable(),
});
export type ConversationSummary = z.infer<typeof conversationSummarySchema>;

export const conversationsResponseSchema = z.object({
  conversations: z.array(conversationSummarySchema),
});
export type ConversationsResponse = z.infer<typeof conversationsResponseSchema>;

/** `GET /api/conversations/{id}`의 턴 하나. 오래된 순이다. */
export const conversationTurnSchema = z.object({
  run_id: z.string(),
  status: runStatusSchema,
  user_text: z.string(),
  assistant_text: z.string().nullable(),
  pending_approval_id: z.string().nullable(),
  created_at: z.string(),
});
export type ConversationTurn = z.infer<typeof conversationTurnSchema>;

export const conversationDetailSchema = z.object({
  id: z.string(),
  turns: z.array(conversationTurnSchema),
});
export type ConversationDetail = z.infer<typeof conversationDetailSchema>;

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
  /** Drive 파일 읽기 권한(선택 권한)을 허용했는지. 없는 응답은 허용하지 않은 것으로 본다. */
  docs_granted: z.boolean().default(false),
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

/** `GET /api/connections/{id}/labels`. 라벨 이름은 외부 문자열이므로 텍스트로만 보여 준다. */
export const mailLabelSchema = z.object({
  id: z.string(),
  name: z.string(),
  kind: z.enum(["system", "user"]),
});
export type MailLabel = z.infer<typeof mailLabelSchema>;
export const labelsResponseSchema = z.object({ labels: z.array(mailLabelSchema) });

/** 색인 범위 하나. `window_days`는 최근 며칠의 메일을 색인하는지다. */
export const indexScopeSchema = z.object({
  id: z.string(),
  label_id: z.string(),
  label_name: z.string(),
  window_days: z.number().int(),
  synced: z.boolean(),
});
export type IndexScope = z.infer<typeof indexScopeSchema>;

/** `GET /api/connections/{id}/index`. 메일 제목·본문은 응답에 없다. */
export const indexStatusSchema = z.object({
  scopes: z.array(indexScopeSchema),
  indexed_mail_count: z.number().int(),
  last_synced_at: z.string().nullable(),
  status: z.enum(["none", "pending", "ok", "error"]),
  error_kind: z.string().nullable(),
  sync_queued: z.boolean(),
});
export type IndexStatus = z.infer<typeof indexStatusSchema>;

/** `POST /api/connections/{id}/index/sync`. 새로 등록했으면 `queued`가 참이다. */
export const syncResponseSchema = z.object({ queued: z.boolean() });
export const deleteScopeResponseSchema = z.object({ id: z.string() });

/**
 * 알림 하나(ADR-0021). `title`은 일정 제목 같은 외부 문자열일 수 있으므로 화면은 텍스트로만 그린다.
 * `kind`의 알려진 값은 `event_reminder`(일정 알림)와 `reminder`(대화 리마인더)다. 모르는 값도 받아서
 * 일반 알림으로 보여 준다.
 */
export const notificationSchema = z.object({
  id: z.string(),
  kind: z.string(),
  title: z.string(),
  due_at: z.string(),
  event_start: z.string().nullable(),
  read: z.boolean(),
  created_at: z.string(),
});
export type NotificationItem = z.infer<typeof notificationSchema>;

/** `GET /api/notifications`. 도착한 알림(최근순, 최대 50개)과 읽지 않은 전체 수. */
export const notificationsResponseSchema = z.object({
  notifications: z.array(notificationSchema),
  unread_count: z.number().int(),
});
export type NotificationsResponse = z.infer<typeof notificationsResponseSchema>;

/** `GET /api/notifications/upcoming`. 예정된 알림(시각순). */
export const upcomingResponseSchema = z.object({ notifications: z.array(notificationSchema) });

export const notificationSettingsSchema = z.object({
  event_reminders_enabled: z.boolean(),
  lead_minutes: z.number().int(),
});
export type NotificationSettings = z.infer<typeof notificationSettingsSchema>;

export const notificationIdResponseSchema = z.object({ id: z.string() });
export const readAllResponseSchema = z.object({ updated: z.number().int() });
