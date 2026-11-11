import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Markdown } from "./Markdown";

describe("제한된 마크다운", () => {
  it("굵게와 글머리 기호·번호 목록을 요소로 만든다", () => {
    const { container } = render(
      <Markdown text={"**오늘 일정**\n- 점심\n- 회의\n1. 첫째\n2. 둘째"} />,
    );
    expect(container.querySelector("strong")).toHaveTextContent("오늘 일정");
    expect(screen.getAllByRole("list")).toHaveLength(2);
    expect(container.querySelectorAll("ul > li")).toHaveLength(2);
    expect(container.querySelectorAll("ol > li")).toHaveLength(2);
  });

  it("문장 중간의 굵게도 처리하고 짝이 없는 기호는 글자로 둔다", () => {
    const { container } = render(<Markdown text={"회의는 **14시**에 있고 **끝나지 않음"} />);
    expect(container.querySelector("strong")).toHaveTextContent("14시");
    expect(container).toHaveTextContent("**끝나지 않음");
  });

  it("스크립트와 HTML 태그는 요소가 아니라 글자로 보인다", () => {
    const { container } = render(
      <Markdown text={"<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n- <b>굵게</b>"} />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container).toHaveTextContent("<script>alert(1)</script>");
    expect(container).toHaveTextContent("<b>굵게</b>");
  });

  it("링크·이미지·코드 블록은 해석하지 않고 글자로 보인다", () => {
    const { container } = render(
      <Markdown text={"[열기](javascript:alert(1))\n![그림](x.png)\n```\ncode\n```"} />,
    );
    expect(container.querySelector("a")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("pre")).toBeNull();
    expect(container.querySelector("code")).toBeNull();
    expect(container).toHaveTextContent("[열기](javascript:alert(1))");
    expect(container).toHaveTextContent("```");
  });

  it("굵게 안의 HTML도 글자다", () => {
    const { container } = render(<Markdown text={"**<i>x</i>**"} />);
    expect(container.querySelector("i")).toBeNull();
    expect(container.querySelector("strong")).toHaveTextContent("<i>x</i>");
  });
});
