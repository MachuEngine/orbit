import { useId, type ReactNode } from "react";
import { Icon } from "./Icon";

type HeadingLevel = 2 | 3 | 4;

interface Props {
  /** 타이틀바에 보이는 제목. 화면낭독기에는 이 제목이 창 이름이 된다. */
  title: string;
  /** 문서 구조에 맞는 heading 레벨. */
  headingLevel: HeadingLevel;
  /** dither(기본): 창 뒤에 어긋나게 깔린 도트 그림자, 다크 바탕용. hard: 검정 각진 그림자, 시트·모래 같은 밝은 판 위 전용. */
  shadow?: "hard" | "dither";
  /** 타이틀바와 여백을 줄인 작은 창. */
  compact?: boolean;
  /** 닫기 버튼. 글자가 없는 버튼이라 label이 꼭 필요하다. */
  close?: { label: string; onClick: () => void };
  className?: string;
  children: ReactNode;
}

/** 레트로 OS 창. 검정 타이틀바와 종이 본문으로 이뤄진 카드·상태 창이다. */
export function RetroWindow({
  title,
  headingLevel,
  shadow = "dither",
  compact = false,
  close,
  className,
  children,
}: Props) {
  const titleId = useId();
  const classes = ["retro", `retro-${shadow}`, compact ? "retro-compact" : "", className ?? ""]
    .filter((name) => name !== "")
    .join(" ");
  const Heading = `h${headingLevel}` as const;
  return (
    <section className={classes} aria-labelledby={titleId}>
      <header className="retro-bar">
        <Heading id={titleId} className="retro-title">
          {title}
        </Heading>
        {close !== undefined && (
          <button
            type="button"
            className="btn btn-ink retro-close"
            aria-label={close.label}
            onClick={close.onClick}
          >
            <Icon name="close" />
          </button>
        )}
      </header>
      <div className="retro-body paper-texture">{children}</div>
    </section>
  );
}
