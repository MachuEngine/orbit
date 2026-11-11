import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom에는 scrollIntoView가 없다.
Element.prototype.scrollIntoView = () => undefined;

afterEach(() => {
  cleanup();
});
