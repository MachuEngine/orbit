import { api as defaultClient, type ApiClient } from "./client";
import {
  approvalSchema,
  connectionsResponseSchema,
  createRunResponseSchema,
  decisionResponseSchema,
  disconnectResponseSchema,
  runSchema,
  sessionSchema,
  startConnectionResponseSchema,
  type Approval,
  type ConnectionsResponse,
  type CreateRunResponse,
  type Decision,
  type DecisionResponse,
  type DisconnectResponse,
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
