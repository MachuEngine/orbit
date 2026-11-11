import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import comboGateSvg from "../../../docs/brand/logo/combo-gate.svg?raw";
import { PRODUCT_NAME } from "../brand";
import { Logo } from "./Logo";

describe("Logo", () => {
  it("확정 로고 원본의 도형을 그대로 그리고 글자색을 따른다", () => {
    const { container } = render(<Logo />);
    const logo = screen.getByRole("img", { name: PRODUCT_NAME });
    expect(logo).toHaveAttribute("fill", "currentColor");
    const originals = [...comboGateSvg.matchAll(/<path d="([^"]+)"/g)].map((match) => match[1]);
    const drawn = [...container.querySelectorAll("path")].map((path) => path.getAttribute("d"));
    expect(drawn).toEqual(originals);
    expect(container.querySelectorAll("circle")).toHaveLength(1);
  });
});
