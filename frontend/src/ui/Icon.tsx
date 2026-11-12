import type { ReactNode } from "react";

export type IconName = "info" | "warning" | "ban" | "success" | "error" | "clock" | "bell";

const SHAPES: Record<IconName, ReactNode> = {
  info: (
    <>
      <circle cx="10" cy="10" r="7.5" />
      <path d="M10 9v5M10 6.2v.1" strokeLinecap="round" />
    </>
  ),
  warning: (
    <>
      <path d="M10 2.5 18 16.5H2z" strokeLinejoin="round" />
      <path d="M10 8v4M10 14.2v.1" strokeLinecap="round" />
    </>
  ),
  ban: (
    <>
      <circle cx="10" cy="10" r="7.5" />
      <path d="M4.7 15.3 15.3 4.7" />
    </>
  ),
  success: (
    <>
      <circle cx="10" cy="10" r="7.5" />
      <path d="m6.5 10.3 2.3 2.3 4.7-5" strokeLinecap="round" strokeLinejoin="round" />
    </>
  ),
  error: (
    <>
      <circle cx="10" cy="10" r="7.5" />
      <path d="m7.5 7.5 5 5M12.5 7.5l-5 5" strokeLinecap="round" />
    </>
  ),
  bell: (
    <>
      <path
        d="M5 14.5h10l-1.2-1.7V9a3.8 3.8 0 0 0-7.6 0v3.8z"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <path d="M8.4 16.8a1.8 1.8 0 0 0 3.2 0" strokeLinecap="round" />
    </>
  ),
  clock: (
    <>
      <circle cx="10" cy="10" r="7.5" />
      <path d="M10 5.8V10l2.8 1.8" strokeLinecap="round" />
    </>
  ),
};

/** 장식용 아이콘. 뜻은 옆의 글자가 전하므로 화면낭독기에는 숨긴다. 색은 글자색을 따른다. */
export function Icon({ name }: { name: IconName }) {
  return (
    <svg
      className="icon"
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      aria-hidden="true"
      focusable="false"
    >
      {SHAPES[name]}
    </svg>
  );
}
