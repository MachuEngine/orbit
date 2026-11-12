import { api as defaultClient, type ApiClient } from "./client";
import {
  approvalSchema,
  connectionsResponseSchema,
  conversationDetailSchema,
  conversationsResponseSchema,
  createRunResponseSchema,
  decisionResponseSchema,
  deleteScopeResponseSchema,
  disconnectResponseSchema,
  indexScopeSchema,
  indexStatusSchema,
  labelsResponseSchema,
  notificationIdResponseSchema,
  notificationSettingsSchema,
  notificationsResponseSchema,
  readAllResponseSchema,
  runSchema,
  sessionSchema,
  startConnectionResponseSchema,
  syncResponseSchema,
  upcomingResponseSchema,
  type Approval,
  type ConnectionsResponse,
  type ConversationDetail,
  type ConversationsResponse,
  type CreateRunResponse,
  type Decision,
  type DecisionResponse,
  type DisconnectResponse,
  type IndexScope,
  type IndexStatus,
  type MailLabel,
  type NotificationItem,
  type NotificationSettings,
  type NotificationsResponse,
  type Run,
  type Session,
} from "./schemas";

const seg = encodeURIComponent;

export function getSession(client: ApiClient = defaultClient): Promise<Session> {
  return client.request("GET", "/api/session", { schema: sessionSchema });
}

/** 개발 환경 전용 임시 로그인(ADR-0013). */
export async function devLogin(client: ApiClient = defaultClient): Promise<void> {
  await client.request("POST", "/api/dev/login");
}

export async function logout(client: ApiClient = defaultClient): Promise<void> {
  await client.request("POST", "/api/logout");
}

export function getConnections(client: ApiClient = defaultClient): Promise<ConnectionsResponse> {
  return client.request("GET", "/api/connections", { schema: connectionsResponseSchema });
}

/** Google 동의 화면 주소를 받는다. 호출한 쪽이 그 주소로 이동한다. */
export async function startGoogleConnection(client: ApiClient = defaultClient): Promise<string> {
  const response = await client.request("POST", "/api/connections/google/start", {
    schema: startConnectionResponseSchema,
  });
  return response.authorization_url;
}

/** 문서(Drive) 읽기 권한을 추가하는 Google 동의 화면 주소를 받는다. */
export async function startDocsScope(
  id: string,
  client: ApiClient = defaultClient,
): Promise<string> {
  const response = await client.request("POST", `/api/connections/${seg(id)}/scopes/docs/start`, {
    schema: startConnectionResponseSchema,
  });
  return response.authorization_url;
}

export function disconnectConnection(
  id: string,
  client: ApiClient = defaultClient,
): Promise<DisconnectResponse> {
  return client.request("DELETE", `/api/connections/${seg(id)}`, {
    schema: disconnectResponseSchema,
  });
}

export function createRun(
  body: { message: string; conversation_id?: string },
  client: ApiClient = defaultClient,
  idempotencyKey?: string,
): Promise<CreateRunResponse> {
  return client.request("POST", "/api/runs", {
    body,
    schema: createRunResponseSchema,
    ...(idempotencyKey === undefined ? {} : { headers: { "Idempotency-Key": idempotencyKey } }),
  });
}

/** 최근 활동 순 대화 목록. */
export function listConversations(
  limit = 50,
  client: ApiClient = defaultClient,
): Promise<ConversationsResponse> {
  return client.request("GET", `/api/conversations?limit=${String(limit)}`, {
    schema: conversationsResponseSchema,
  });
}

/** 대화의 턴을 오래된 순으로 받는다. 없거나 다른 계정의 대화면 404(`not_found`)다. */
export function getConversation(
  id: string,
  client: ApiClient = defaultClient,
): Promise<ConversationDetail> {
  return client.request("GET", `/api/conversations/${seg(id)}`, {
    schema: conversationDetailSchema,
  });
}

export function getRun(runId: string, client: ApiClient = defaultClient): Promise<Run> {
  return client.request("GET", `/api/runs/${seg(runId)}`, { schema: runSchema });
}

export function runEventsUrl(runId: string): string {
  return `/api/runs/${seg(runId)}/events`;
}

export function getApproval(
  approvalId: string,
  client: ApiClient = defaultClient,
): Promise<Approval> {
  return client.request("GET", `/api/approvals/${seg(approvalId)}`, { schema: approvalSchema });
}

export function decideApproval(
  approvalId: string,
  decision: Decision,
  client: ApiClient = defaultClient,
): Promise<DecisionResponse> {
  return client.request("POST", `/api/approvals/${seg(approvalId)}/decision`, {
    body: { decision },
    schema: decisionResponseSchema,
  });
}

const indexPath = (id: string) => `/api/connections/${seg(id)}/index`;

/** 색인할 수 있는 메일 라벨 목록. 커넥터 오류면 `ApiError.detail`에 오류 종류가 온다. */
export async function getMailLabels(
  id: string,
  client: ApiClient = defaultClient,
): Promise<MailLabel[]> {
  const response = await client.request("GET", `/api/connections/${seg(id)}/labels`, {
    schema: labelsResponseSchema,
  });
  return response.labels;
}

export function getIndexStatus(
  id: string,
  client: ApiClient = defaultClient,
): Promise<IndexStatus> {
  return client.request("GET", indexPath(id), { schema: indexStatusSchema });
}

export function addIndexScope(
  id: string,
  body: { label_id: string; window_days: number },
  client: ApiClient = defaultClient,
): Promise<IndexScope> {
  return client.request("POST", `${indexPath(id)}/scopes`, { body, schema: indexScopeSchema });
}

export async function deleteIndexScope(
  id: string,
  scopeId: string,
  client: ApiClient = defaultClient,
): Promise<void> {
  await client.request("DELETE", `${indexPath(id)}/scopes/${seg(scopeId)}`, {
    schema: deleteScopeResponseSchema,
  });
}

/** "지금 동기화". 이미 대기 중이면 `queued`가 거짓이다. */
export async function syncIndexNow(
  id: string,
  client: ApiClient = defaultClient,
): Promise<boolean> {
  const response = await client.request("POST", `${indexPath(id)}/sync`, {
    schema: syncResponseSchema,
  });
  return response.queued;
}

// ---------------------------------------------------------------- 알림 (ADR-0021)

/** 도착한 알림(최근순, 최대 50개)과 읽지 않은 전체 수. */
export function getNotifications(
  client: ApiClient = defaultClient,
): Promise<NotificationsResponse> {
  return client.request("GET", "/api/notifications", { schema: notificationsResponseSchema });
}

/** 예정된 알림(시각순). */
export async function getUpcomingNotifications(
  client: ApiClient = defaultClient,
): Promise<NotificationItem[]> {
  const response = await client.request("GET", "/api/notifications/upcoming", {
    schema: upcomingResponseSchema,
  });
  return response.notifications;
}

export async function markNotificationRead(
  id: string,
  client: ApiClient = defaultClient,
): Promise<void> {
  await client.request("POST", `/api/notifications/${seg(id)}/read`, {
    schema: notificationIdResponseSchema,
  });
}

export async function markAllNotificationsRead(client: ApiClient = defaultClient): Promise<void> {
  await client.request("POST", "/api/notifications/read-all", { schema: readAllResponseSchema });
}

/** 예정된 알림을 취소한다. 이미 도착했거나 취소됐으면 409(`conflict`)다. */
export async function cancelNotification(
  id: string,
  client: ApiClient = defaultClient,
): Promise<void> {
  await client.request("DELETE", `/api/notifications/${seg(id)}`, {
    schema: notificationIdResponseSchema,
  });
}

export function getNotificationSettings(
  client: ApiClient = defaultClient,
): Promise<NotificationSettings> {
  return client.request("GET", "/api/notification-settings", {
    schema: notificationSettingsSchema,
  });
}

export function putNotificationSettings(
  body: NotificationSettings,
  client: ApiClient = defaultClient,
): Promise<NotificationSettings> {
  return client.request("PUT", "/api/notification-settings", {
    body,
    schema: notificationSettingsSchema,
  });
}
