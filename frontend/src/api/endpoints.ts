import { api as defaultClient, type ApiClient } from "./client";
import {
  approvalSchema,
  createRunResponseSchema,
  decisionResponseSchema,
  runSchema,
  sessionSchema,
  type Approval,
  type CreateRunResponse,
  type Decision,
  type DecisionResponse,
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

export function createRun(
  body: { message: string; conversation_id?: string },
  client: ApiClient = defaultClient,
): Promise<CreateRunResponse> {
  return client.request("POST", "/api/runs", { body, schema: createRunResponseSchema });
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
