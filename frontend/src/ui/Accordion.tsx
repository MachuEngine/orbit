import type { ReactNode } from "react";
import { Icon } from "./Icon";

/** 네이티브 details/summary 아코디언. 키보드와 화면낭독기 동작은 브라우저가 맡는다. */
export function Accordion({
  summary,
  defaultOpen = false,
  children,
}: {
  summary: string;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  return (
    <details className="accordion" open={defaultOpen}>
      <summary className="accordion-summary">
        <span>{summary}</span>
        <span className="accordion-chevron">
          <Icon name="chevron" />
        </span>
      </summary>
      <div className="accordion-body">{children}</div>
    </details>
  );
}
