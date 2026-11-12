// SSE 이벤트 형태는 backend/src/orbit/runflow/nodes.py가 남기는 run_event 기준이다.
import { z } from "zod";
import { executionStatusSchema, runStatusSchema } from "../api/schemas";

const statusEvent = z.object({
  type: z.literal("status"),
  payload: z.object({ status: runStatusSchema }),
});
const errorEvent = z.object({
  type: z.literal("error"),
  payload: z.object({ error_kind: z.string() }),
});
const assistantTextEvent = z.object({
  type: z.literal("assistant_text"),
  payload: z.object({ step: z.number(), text: z.string() }),
});
const approvalRequestedEvent = z.object({
  type: z.literal("approval_requested"),
  payload: z.object({
    step: z.number(),
    approval_id: z.string(),
    tool_name: z.string(),
    summary: z.record(z.string(), z.unknown()),
  }),
});
const executionResultEvent = z.object({
  type: z.literal("execution_result"),
  payload: z.object({
    step: z.number(),
    approval_id: z.string(),
    status: executionStatusSchema,
    error_kind: z.string().optional(),
  }),
});

const connectorNoticeEvent = z.object({
  type: z.literal("connector_notice"),
  payload: z.object({
    step: z.number(),
    service: z.enum(["calendar", "mail"]),
    kind: z.string(),
    reason: z.string(),
  }),
});

const eventBodySchema = z.discriminatedUnion("type", [
  statusEvent,
  errorEvent,
  assistantTextEvent,
  approvalRequestedEvent,
  executionResultEvent,
  connectorNoticeEvent,
]);

export type RunEventBody = z.infer<typeof eventBodySchema>;
export type RunEvent = RunEventBody & { seq: number };

export const RUN_EVENT_TYPES = [
  "status",
  "error",
  "assistant_text",
  "approval_requested",
  "execution_result",
  "connector_notice",
] as const;

export type ParsedEvent =
  | { kind: "event"; event: RunEvent }
  | { kind: "unknown_type" } // 이 버전이 모르는 이벤트 종류. 무시한다
  | { kind: "invalid" }; // 알려진 종류인데 형태가 다르다

/** SSE의 `id`(seq), `event`(type), `data`(JSON payload)를 검증해 이벤트로 바꾼다. */
export function parseRunEvent(rawSeq: string, type: string, rawData: string): ParsedEvent {
  const seq = Number(rawSeq);
  if (!Number.isInteger(seq) || seq < 1) return { kind: "invalid" };
  if (!(RUN_EVENT_TYPES as readonly string[]).includes(type)) return { kind: "unknown_type" };
  let payload: unknown;
  try {
    payload = JSON.parse(rawData);
  } catch {
    return { kind: "invalid" };
  }
  const parsed = eventBodySchema.safeParse({ type, payload });
  if (!parsed.success) return { kind: "invalid" };
  return { kind: "event", event: { ...parsed.data, seq } };
}
