import type { ReactNode } from "react";

/**
 * 비서 응답의 제한된 마크다운. 제목(`# `), 굵게(`**글자**`), 인라인 코드(`` `x` ``), 목록(`- `, `1. `)만
 * React 요소로 만든다. HTML 문자열을 삽입하지 않는다.
 * 링크 `[글자](주소)`는 `<a>`로 만들지 않고 글자 뒤에 주소를 그대로 보여 준다(D6-1: 외부 콘텐츠의
 * 주소를 눌러 열거나 자동으로 불러오게 하지 않는다). 이미지·코드 블록·HTML 태그는 글자 그대로 보인다.
 */

type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "text"; lines: string[] }
  | { kind: "ul"; items: string[] }
  | { kind: "ol"; items: string[] };

const HEADING = /^(#{1,6}) +(\S.*)$/;
const BULLET = /^[-] (.*)$/;
const ORDERED = /^\d{1,3}\. (.*)$/;

export function parseBlocks(source: string): Block[] {
  const blocks: Block[] = [];
  for (const line of source.split("\n")) {
    const heading = HEADING.exec(line);
    if (heading !== null) {
      blocks.push({ kind: "heading", level: (heading[1] ?? "#").length, text: heading[2] ?? "" });
      continue;
    }
    const bullet = BULLET.exec(line);
    const ordered = bullet === null ? ORDERED.exec(line) : null;
    const kind = bullet !== null ? "ul" : ordered !== null ? "ol" : "text";
    const last = blocks[blocks.length - 1];
    if (kind === "text") {
      if (last?.kind === "text") last.lines.push(line);
      else blocks.push({ kind: "text", lines: [line] });
      continue;
    }
    const item = (bullet ?? ordered)?.[1] ?? "";
    if (last?.kind === kind) last.items.push(item);
    else blocks.push({ kind, items: [item] });
  }
  return blocks;
}

// 주소는 공백 없는 글자와 한 단계 괄호 쌍만 허용한다. 그 밖의 모양은 해석하지 않고 글자로 둔다.
const URL_BODY = String.raw`(?:[^()\s]|\([^()\s]*\))+`;
const INLINE = new RegExp(
  [
    "`([^`\\n]+)`", // 1: 코드
    String.raw`!\[[^\[\]\n]*\]\(` + URL_BODY + String.raw`\)`, // 이미지: 글자로 둔다
    String.raw`\[([^\[\]\n]*)\]\(` + "(" + URL_BODY + ")" + String.raw`\)`, // 2: 링크 글자, 3: 주소
    String.raw`\*\*([^*\n]+)\*\*`, // 4: 굵게
  ].join("|"),
  "g",
);

function inline(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let from = 0;
  for (const match of text.matchAll(INLINE)) {
    if (match.index > from) nodes.push(text.slice(from, match.index));
    const [whole, code, label, url, bold] = match;
    if (code !== undefined) {
      nodes.push(<code key={match.index}>{code}</code>);
    } else if (url !== undefined) {
      nodes.push(
        <span key={match.index} className="md-link">
          {inline(label ?? "")}
          {label !== "" && " "}
          <span className="md-url">({url})</span>
        </span>,
      );
    } else if (bold !== undefined) {
      nodes.push(<strong key={match.index}>{inline(bold)}</strong>);
    } else {
      nodes.push(whole); // 이미지 문법은 해석하지 않고 원문 글자로 둔다
    }
    from = match.index + whole.length;
  }
  if (from < text.length) nodes.push(text.slice(from));
  return nodes;
}

export function Markdown({ text }: { text: string }) {
  return (
    <>
      {parseBlocks(text).map((block, index) => {
        if (block.kind === "heading") {
          // 화면에 숨긴 h1("대화")이 있으므로 응답 제목은 h2부터 쓰고, 단계를 건너뛰지 않게 h5까지만 쓴다.
          const Heading = `h${Math.min(block.level + 1, 5)}` as "h2" | "h3" | "h4" | "h5";
          return (
            <Heading key={index} className="md-heading">
              {inline(block.text)}
            </Heading>
          );
        }
        if (block.kind === "text") return <p key={index}>{inline(block.lines.join("\n"))}</p>;
        const Tag = block.kind;
        return (
          <Tag key={index} className="md-list">
            {block.items.map((item, i) => (
              <li key={i}>{inline(item)}</li>
            ))}
          </Tag>
        );
      })}
    </>
  );
}
