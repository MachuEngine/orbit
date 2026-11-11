import type { Approval, Provenance } from "../api/schemas";
import { isWarnedProvenance } from "./logic";

/** 승인 카드에 보여 줄 사람 한 명(수신자·참석자). 출처가 외부 본문이나 모델이면 `flagged`다. */
export interface Person {
  email: string;
  source: Provenance | null;
  flagged: boolean;
}

export type DetailRow =
  | { kind: "text"; label: string; value: string }
  | { kind: "people"; label: string; people: Person[] }
  | {
      kind: "longtext";
      label: string;
      /** 한 줄로 줄인 앞부분 */
      excerpt: string;
      /** 전체 본문 */
      text: string;
      /** true면 접어 두고, false면 본문이 짧아 그대로 보여 준다 */
      collapsible: boolean;
    };

export interface ApprovalView {
  rows: DetailRow[];
  /** 인자 목록에 없는데 출처 경고가 붙은 주소. 경고를 빠뜨리지 않으려고 따로 보여 준다. */
  strayWarnings: Person[];
  /** 승인 인자에 있지만 이 화면이 보여 주지 않은 항목 이름 */
  hiddenKeys: string[];
}

type ApprovalInput = Pick<Approval, "tool_name" | "args" | "summary" | "provenance"> & {
  target?: Approval["target"];
};

/** 화면에 보이지 않아도 되는 내부 식별자. 제공자 ID는 일부러 보여 주지 않는다. */
const INTERNAL_KEYS = ["kind", "calendar_id", "event_id"];
/** 도구별로 이 화면이 보여 주는 인자 이름 */
const SHOWN_KEYS: Record<string, string[]> = {
  send_mail: ["to", "cc", "subject", "body", "reply_to_thread_id"],
  create_event: ["title", "start", "end", "attendees", "location", "description"],
  update_event_time: ["start", "end"],
  cancel_event: [],
};

const EXCERPT_LENGTH = 80;
const COLLAPSE_OVER_LENGTH = 120;
const COLLAPSE_OVER_LINES = 3;

function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function strList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function person(email: string, provenance: Approval["provenance"]): Person {
  const source = provenance[email.toLowerCase()] ?? null;
  // 출처를 알 수 없는 주소도 경고한다(확인하지 못한 값은 안전하다고 보지 않는다).
  return { email, source, flagged: source === null || isWarnedProvenance(source) };
}

/** 이 출처가 왜 확인이 필요한지 한 줄로 설명한다. 확인이 필요 없는 출처는 null이다. */
export function provenanceReason(source: Provenance | null): string | null {
  switch (source) {
    case "external_content":
      return "메일·문서 본문에서 가져온 주소입니다. 직접 말씀하신 주소가 맞는지 확인해 주세요.";
    case "model":
      return "직접 말씀하지 않아 비서가 만든 주소입니다. 맞는지 확인해 주세요.";
    default:
      return null;
  }
}

/** 수신자·참석자 주소에 붙이는 이유. 출처가 없으면(null) 확인하지 못했다고 알린다. */
export function personReason(person: Person): string | null {
  return person.source === null && person.flagged
    ? "출처를 확인하지 못한 주소입니다. 직접 말씀하신 주소가 맞는지 확인해 주세요."
    : provenanceReason(person.source);
}

/** 출처가 확인된 주소 옆에 붙이는 짧은 말. */
export function provenanceTag(source: Provenance | null): string | null {
  switch (source) {
    case "user":
      return "직접 입력";
    case "workspace_data":
      return "내 메일·일정에 있는 주소";
    default:
      return null;
  }
}

/** 시각 범위를 "11월 12일 (목) 14:30 – 15:00 (GMT+9)"처럼 쓴다. 해석할 수 없으면 받은 값을 그대로 쓴다. */
export function formatTimeRange(start: string, end: string, timeZone?: string): string {
  const startDate = new Date(start);
  const endDate = new Date(end);
  if (Number.isNaN(startDate.getTime()) || Number.isNaN(endDate.getTime()))
    return `${start} – ${end}`;
  const base = { timeZone } satisfies Intl.DateTimeFormatOptions;
  const dayOnly = new Intl.DateTimeFormat("ko-KR", {
    ...base,
    year: "numeric",
    month: "numeric",
    day: "numeric",
  });
  const full = new Intl.DateTimeFormat("ko-KR", {
    ...base,
    month: "long",
    day: "numeric",
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });
  const clock = new Intl.DateTimeFormat("ko-KR", {
    ...base,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZoneName: "short",
  });
  const endParts = clock.formatToParts(endDate);
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    endParts.find((p) => p.type === type)?.value ?? "";
  const zone = part("timeZoneName");
  const endText =
    dayOnly.format(startDate) === dayOnly.format(endDate)
      ? `${part("hour")}:${part("minute")}`
      : full.format(endDate);
  return `${full.format(startDate)} – ${endText}${zone === "" ? "" : ` (${zone})`}`;
}

function longText(label: string, text: string): DetailRow {
  const oneLine = text.replace(/\s+/g, " ").trim();
  const excerpt =
    oneLine.length > EXCERPT_LENGTH ? `${oneLine.slice(0, EXCERPT_LENGTH)}…` : oneLine;
  const lines = text.split("\n").length;
  const collapsible = text.length > COLLAPSE_OVER_LENGTH || lines > COLLAPSE_OVER_LINES;
  return { kind: "longtext", label, excerpt, text, collapsible };
}

function timeRow(label: string, args: Record<string, unknown>): DetailRow | null {
  const start = str(args.start);
  const end = str(args.end);
  return start !== null && end !== null
    ? { kind: "text", label, value: formatTimeRange(start, end) }
    : null;
}

function eventTargetRow(
  summary: Record<string, unknown>,
  target: Approval["target"] | undefined,
): DetailRow {
  // 서버가 준 대상 요약(제목)을 쓴다. 없으면 모델과 같은 핸들(E1 등)만 보여 준다. 제공자 ID는 오지 않는다.
  if (target?.kind === "event") return { kind: "text", label: "대상 일정", value: target.title };
  const handle = str(summary.handle);
  return {
    kind: "text",
    label: "대상 일정",
    value: handle === null ? "지정한 일정" : `일정 ${handle}`,
  };
}

/** 승인 인자를 사람이 읽는 항목으로 바꾼다. JSON 원문은 보여 주지 않는다. */
export function describeApproval(approval: ApprovalInput): ApprovalView {
  const { args, summary, provenance, target } = approval;
  const rows: DetailRow[] = [];
  const hiddenKeys: string[] = [];
  const shown = new Set<string>();
  const people = (label: string, emails: string[]) => {
    if (emails.length === 0) return;
    for (const email of emails) shown.add(email.toLowerCase());
    rows.push({ kind: "people", label, people: emails.map((email) => person(email, provenance)) });
  };
  const push = (row: DetailRow | null) => {
    if (row !== null) rows.push(row);
  };

  switch (approval.tool_name) {
    case "send_mail": {
      people("받는 사람", strList(args.to));
      people("참조", strList(args.cc));
      const subject = str(args.subject);
      if (subject !== null) rows.push({ kind: "text", label: "제목", value: subject });
      const body = str(args.body);
      if (body !== null) rows.push(longText("본문", body));
      if (str(args.reply_to_thread_id) !== null) {
        rows.push(
          target?.kind === "thread"
            ? { kind: "text", label: "답장할 대화", value: target.subject }
            : { kind: "text", label: "종류", value: "받은 메일 대화에 답장" },
        );
      }
      break;
    }
    case "create_event": {
      const title = str(args.title);
      if (title !== null) rows.push({ kind: "text", label: "제목", value: title });
      push(timeRow("시간", args));
      people("참석자", strList(args.attendees));
      const location = str(args.location);
      if (location !== null) rows.push({ kind: "text", label: "장소", value: location });
      const description = str(args.description);
      if (description !== null) rows.push(longText("설명", description));
      break;
    }
    case "update_event_time": {
      rows.push(eventTargetRow(summary, target));
      if (target?.kind === "event") {
        rows.push({
          kind: "text",
          label: "변경 전",
          value: formatTimeRange(target.start, target.end),
        });
        push(timeRow("변경 후", args));
      } else {
        push(timeRow("새 시간", args));
      }
      break;
    }
    case "cancel_event": {
      rows.push(eventTargetRow(summary, target));
      if (target?.kind === "event") {
        rows.push({
          kind: "text",
          label: "일정 시간",
          value: formatTimeRange(target.start, target.end),
        });
      }
      break;
    }
    default: {
      // 모르는 동작은 문자열·숫자 인자만 이름과 값으로 보여 준다. 객체는 펼치지 않는다.
      for (const [key, value] of Object.entries(args)) {
        if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
          rows.push({ kind: "text", label: key, value: String(value) });
        } else {
          hiddenKeys.push(key);
        }
      }
    }
  }

  // 알려진 도구에 이 화면이 모르는 인자가 들어 있으면 숨겨진 값으로 알린다.
  const known = SHOWN_KEYS[approval.tool_name];
  if (known !== undefined) {
    for (const key of Object.keys(args)) {
      if (!known.includes(key) && !INTERNAL_KEYS.includes(key)) hiddenKeys.push(key);
    }
  }

  const strayWarnings = Object.keys(provenance)
    .filter((email) => !shown.has(email.toLowerCase()))
    .map((email) => person(email, provenance))
    .filter((p) => p.flagged);
  return { rows, strayWarnings, hiddenKeys };
}

/** 카드 전체에서 출처 경고가 붙은 주소가 하나라도 있는지. */
export function hasProvenanceWarning(view: ApprovalView): boolean {
  return (
    view.strayWarnings.length > 0 ||
    view.rows.some((row) => row.kind === "people" && row.people.some((p) => p.flagged))
  );
}
