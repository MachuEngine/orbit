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

  it("이미지·코드 블록은 해석하지 않고 글자로 보인다", () => {
    const { container } = render(<Markdown text={"![그림](x.png)\n```\ncode\n```"} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("pre")).toBeNull();
    expect(container.querySelector("code")).toBeNull();
    expect(container).toHaveTextContent("![그림](x.png)");
    expect(container).toHaveTextContent("```");
  });

  it("# 제목 줄을 제목 요소로 만들고 단계는 h2부터 h5까지 쓴다", () => {
    const { container } = render(
      <Markdown text={"# 오늘\n## 일정\n#### 깊은 제목\n###### 가장 깊음\n#없음\n####### 일곱"} />,
    );
    expect(container.querySelector("h2")).toHaveTextContent("오늘");
    expect(container.querySelector("h3")).toHaveTextContent("일정");
    expect(container.querySelector("h5")).toHaveTextContent("깊은 제목");
    expect(container.querySelectorAll("h5")).toHaveLength(2);
    expect(container.querySelector("h1")).toBeNull();
    expect(container.querySelector("h6")).toBeNull();
    expect(container).toHaveTextContent("#없음");
    expect(container).toHaveTextContent("####### 일곱");
  });

  it("링크는 a 요소가 아니고 글자 뒤에 주소가 그대로 보인다", () => {
    const { container } = render(
      <Markdown text={"자세히 [공지](https://example.com/a?b=1) 참고"} />,
    );
    expect(container.querySelector("a")).toBeNull();
    expect(container.querySelector("[href]")).toBeNull();
    expect(container).toHaveTextContent("자세히 공지 (https://example.com/a?b=1) 참고");
    expect(container.querySelector(".md-url")).toHaveTextContent("(https://example.com/a?b=1)");
  });

  it("javascript: 주소도 누를 수 없는 글자로만 보인다", () => {
    const { container } = render(<Markdown text={"[열기](javascript:alert(1))"} />);
    expect(container.querySelector("a")).toBeNull();
    expect(container.querySelector("[href]")).toBeNull();
    expect(container).toHaveTextContent("열기 (javascript:alert(1))");
  });

  it("링크 글자가 비어 있으면 주소만 보인다", () => {
    const { container } = render(<Markdown text={"[](https://example.com)"} />);
    expect(container).toHaveTextContent("(https://example.com)");
    expect(container.querySelector("a")).toBeNull();
  });

  it("이미지 안의 링크와 짝이 안 맞는 괄호는 요소를 만들지 않는다", () => {
    const { container } = render(
      <Markdown
        text={
          "[![로고](https://x.test/i.png)](https://x.test)\n[끊김](https://x.test\n[a](b c)\n[x]((y))"
        }
      />,
    );
    expect(container.querySelector("a")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container).toHaveTextContent("[끊김](https://x.test");
    expect(container).toHaveTextContent("[a](b c)");
    expect(container).toHaveTextContent("x ((y))"); // 괄호 쌍이라 주소로 보지만 글자일 뿐이다
  });

  it("주소 안의 괄호 한 쌍은 주소에 포함한다", () => {
    const { container } = render(<Markdown text={"[위키](https://x.test/a_(b))."} />);
    expect(container.querySelector(".md-url")).toHaveTextContent("(https://x.test/a_(b))");
  });

  it("인라인 코드는 code 요소이고 안의 기호와 HTML은 글자다", () => {
    const { container } = render(<Markdown text={"`**x**` 와 `<b>y</b>` 그리고 `짝없음"} />);
    const codes = container.querySelectorAll("code");
    expect(codes).toHaveLength(2);
    expect(codes[0]).toHaveTextContent("**x**");
    expect(container.querySelector("strong")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container).toHaveTextContent("`짝없음");
  });

  it("제목 안의 굵게와 링크도 같은 규칙을 따른다", () => {
    const { container } = render(<Markdown text={"## **큰** [링크](u)"} />);
    expect(container.querySelector("h3 strong")).toHaveTextContent("큰");
    expect(container.querySelector("a")).toBeNull();
    expect(container.querySelector("h3")).toHaveTextContent("큰 링크 (u)");
  });

  it("굵게 안의 HTML도 글자다", () => {
    const { container } = render(<Markdown text={"**<i>x</i>**"} />);
    expect(container.querySelector("i")).toBeNull();
    expect(container.querySelector("strong")).toHaveTextContent("<i>x</i>");
  });

  it("굵게 안의 링크도 주소 노출 방식으로 보이고 쉼표·따옴표가 있어도 된다", () => {
    const { container } = render(
      <Markdown text={'- **[a, ‘b’ "c"](https://x.y/?id=1)** — 설명'} />,
    );
    expect(container.querySelector("a")).toBeNull();
    expect(container.querySelector("[href]")).toBeNull();
    expect(container.querySelector("strong .md-url")).toHaveTextContent("(https://x.y/?id=1)");
    expect(container.querySelector("li")).toHaveTextContent(
      'a, ‘b’ "c" (https://x.y/?id=1) — 설명',
    );
  });

  it("링크 글자 안의 굵게와 굵게 안의 코드를 해석한다", () => {
    const { container } = render(<Markdown text={"[**a**](u) **`x`**"} />);
    expect(container.querySelector(".md-link strong")).toHaveTextContent("a");
    expect(container.querySelector("strong code")).toHaveTextContent("x");
    expect(container.querySelector("a")).toBeNull();
  });

  it("굵게 짝이 안 맞아도 안쪽 링크는 해석하고 별표는 글자로 둔다", () => {
    const { container } = render(<Markdown text={"**[a](u) 끝나지 않음"} />);
    expect(container.querySelector("strong")).toBeNull();
    expect(container.querySelector(".md-url")).toHaveTextContent("(u)");
    expect(container).toHaveTextContent("**a (u) 끝나지 않음");
  });
});
