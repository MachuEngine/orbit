import type { ReactNode } from "react";

/**
 * 비서 응답의 제한된 마크다운. 굵게(`**글자**`)와 목록(`- `, `1. `)만 React 요소로 만든다.
 * HTML 문자열을 삽입하지 않는다. 링크·이미지·코드 블록·HTML 태그는 해석하지 않고 글자 그대로 보인다.
 */

type Block =
  | { kind: "text"; lines: string[] }
  | { kind: "ul"; items: string[] }
  | { kind: "ol"; items: string[] };

const BULLET = /^[-] (.*)$/;
const ORDERED = /^\d{1,3}\. (.*)$/;

export function parseBlocks(source: string): Block[] {
  const blocks: Block[] = [];
  for (const line of source.split("\n")) {
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

const BOLD = /\*\*([^*\n]+)\*\*/g;

function inline(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let from = 0;
  for (const match of text.matchAll(BOLD)) {
    if (match.index > from) nodes.push(text.slice(from, match.index));
    nodes.push(<strong key={match.index}>{match[1]}</strong>);
    from = match.index + match[0].length;
  }
  if (from < text.length) nodes.push(text.slice(from));
  return nodes;
}

export function Markdown({ text }: { text: string }) {
  return (
    <>
      {parseBlocks(text).map((block, index) => {
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
