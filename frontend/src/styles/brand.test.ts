import { describe, expect, it } from "vitest";
import brandGuide from "../../../docs/brand/brand-guide.md?raw";
import tokensCss from "../../../docs/brand/tokens.css?raw";
import tokensJson from "../../../docs/brand/tokens.json?raw";

// ── tokens.css와 tokens.json 동기화 ───────────────────────────────────────

interface Leaf {
  $value: string | number;
}
interface Tree {
  [key: string]: Tree | Leaf;
}

const tokens = JSON.parse(tokensJson) as Tree;

function isLeaf(node: Tree | Leaf): node is Leaf {
  return "$value" in node;
}

function at(path: string): Leaf {
  let node: Tree | Leaf = tokens;
  for (const key of path.split(".")) {
    if (isLeaf(node)) throw new Error(`${path}: 토큰 경로가 잘못됐다`);
    const next: Tree | Leaf | undefined = node[key];
    if (next === undefined) throw new Error(`${path}: tokens.json에 없다`);
    node = next;
  }
  if (!isLeaf(node)) throw new Error(`${path}: 값이 아니라 묶음이다`);
  return node;
}

function resolve(value: string | number): string {
  const text = String(value);
  const ref = /^\{(.+)\}$/.exec(text);
  return ref?.[1] === undefined ? text : resolve(at(ref[1]).$value);
}

function leaves(path: string): [string, string][] {
  let node: Tree | Leaf = tokens;
  for (const key of path.split(".")) {
    if (isLeaf(node)) throw new Error(`${path}: 토큰 경로가 잘못됐다`);
    const next: Tree | Leaf | undefined = node[key];
    if (next === undefined) throw new Error(`${path}: tokens.json에 없다`);
    node = next;
  }
  if (isLeaf(node)) throw new Error(`${path}: 묶음이 아니라 값이다`);
  return Object.entries(node).flatMap(([name, leaf]) =>
    isLeaf(leaf) ? [[name, resolve(leaf.$value)] as [string, string]] : [],
  );
}

/** tokens.json이 정의하는 값을 CSS 변수 이름으로 펼친다. 모드와 무관한 값. */
function commonVars(): Record<string, string> {
  const vars: Record<string, string> = {};
  const add = (prefix: string, path: string) => {
    for (const [name, value] of leaves(path)) vars[`--${prefix}-${name}`] = value;
  };
  add("font", "font.family");
  add("font-weight", "font.weight");
  add("text", "font.size");
  add("leading", "font.leading");
  add("tracking", "font.tracking");
  add("measure", "font.measure");
  add("space", "space");
  add("radius", "radius");
  add("motion", "motion.duration");
  add("ease", "motion.easing");
  return vars;
}

const colorVars = (mode: "light" | "dark") =>
  Object.fromEntries(leaves(`color.${mode}`).map(([name, value]) => [`--color-${name}`, value]));
const shadowVars = (mode: "light" | "dark") =>
  Object.fromEntries(leaves(`shadow.${mode}`).map(([name, value]) => [`--shadow-${name}`, value]));

function declarations(block: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const match of block.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    result[match[1] ?? ""] = (match[2] ?? "").replace(/\s+/g, " ").trim();
  }
  return result;
}

function block(selector: RegExp): Record<string, string> {
  const match = selector.exec(tokensCss);
  if (match?.[1] === undefined) throw new Error(`tokens.css에서 ${selector.source}를 찾지 못했다`);
  return declarations(match[1]);
}

const lower = (vars: Record<string, string>) =>
  Object.fromEntries(Object.entries(vars).map(([key, value]) => [key, value.toLowerCase()]));

// 세 블록 모두 중첩이 없어 `[^}]*`로 읽는다.
const rootCss = block(/^:root\s*\{([^}]*)\}/m);
const mediaDarkCss = block(/:root:not\(\[data-theme="light"\]\)\s*\{([^}]*)\}/);
const darkCss = block(/:root\[data-theme="dark"\]\s*\{([^}]*)\}/);

describe("tokens.css와 tokens.json", () => {
  it("공통 토큰과 밝은 모드 값이 같다", () => {
    expect(lower(rootCss)).toEqual(
      lower({ ...commonVars(), ...colorVars("light"), ...shadowVars("light") }),
    );
  });

  it("어두운 모드 값이 같다(시스템 설정과 data-theme 양쪽)", () => {
    const expected = lower({ ...colorVars("dark"), ...shadowVars("dark") });
    expect(lower(darkCss)).toEqual(expected);
    expect(lower(mediaDarkCss)).toEqual(expected);
  });
});

// ── 화면 코드가 토큰만 쓰는지 ─────────────────────────────────────────────

const cssFiles = import.meta.glob<string>(["/src/**/*.css", "!/src/styles/fonts.css"], {
  query: "?raw",
  import: "default",
  eager: true,
});

const sourceFiles = import.meta.glob<string>(["/src/**/*.{ts,tsx}", "!/src/**/*.test.{ts,tsx}"], {
  query: "?raw",
  import: "default",
  eager: true,
});

function only(files: Record<string, string>, path: string): string {
  const text = files[path];
  if (text === undefined) throw new Error(`${path}를 찾지 못했다`);
  return text;
}

const fontsCss = only(
  import.meta.glob<string>("/src/styles/fonts.css", {
    query: "?raw",
    import: "default",
    eager: true,
  }),
  "/src/styles/fonts.css",
);

function stripComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

describe("화면 스타일 규칙", () => {
  it("스타일 파일이 있다", () => {
    expect(Object.keys(cssFiles).length).toBeGreaterThanOrEqual(4);
  });

  it("컴포넌트에 hex·rgb·hsl 색을 직접 쓰지 않는다", () => {
    const all = { ...cssFiles, ...sourceFiles };
    for (const [file, text] of Object.entries(all)) {
      const code = stripComments(text);
      expect(code, file).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
      expect(code, file).not.toMatch(/\b(rgb|rgba|hsl|hsla)\(/);
    }
  });

  it("글자 크기·굵기·글꼴은 토큰 변수로만 지정한다", () => {
    for (const [file, text] of Object.entries(cssFiles)) {
      const code = stripComments(text);
      for (const match of code.matchAll(/font-size:\s*([^;]+);/g)) {
        expect(match[1], file).toMatch(/^var\(--text-/);
      }
      for (const match of code.matchAll(/font-weight:\s*([^;]+);/g)) {
        expect(match[1], file).toMatch(/^var\(--font-weight-/);
      }
      for (const match of code.matchAll(/font-family:\s*([^;]+);/g)) {
        expect(match[1], file).toMatch(/^var\(--font-/);
      }
    }
    for (const [file, text] of Object.entries(sourceFiles)) {
      expect(stripComments(text), file).not.toMatch(/fontSize|fontFamily|style=\{/);
    }
  });

  it("쓰는 글꼴 굵기는 모두 자체 호스팅한 굵기 안에 있다", () => {
    const shipped = new Set(
      [...fontsCss.matchAll(/font-weight:\s*(\d+);/g)].map((match) => match[1] ?? ""),
    );
    const weightTokens = commonVars();
    for (const [file, text] of Object.entries(cssFiles)) {
      for (const match of stripComments(text).matchAll(/var\((--font-weight-[\w-]+)\)/g)) {
        expect(shipped.has(weightTokens[match[1] ?? ""] ?? ""), `${file} ${match[1] ?? ""}`).toBe(
          true,
        );
      }
    }
  });

  it("포커스 표시를 없애지 않는다", () => {
    for (const [file, text] of Object.entries(cssFiles)) {
      expect(stripComments(text), file).not.toMatch(/outline:\s*(none|0)\b/);
    }
  });

  it("모션 줄이기 설정을 지원한다", () => {
    const all = Object.values(cssFiles).join("\n");
    expect(all).toContain("prefers-reduced-motion: reduce");
  });

  it("외부 주소를 불러오지 않는다", () => {
    for (const [file, text] of Object.entries({ ...cssFiles, ...sourceFiles })) {
      const code = stripComments(text);
      expect(code, file).not.toMatch(/https?:\/\//);
      expect(code, file).not.toMatch(/@import/);
    }
  });

  it("제품 이름 문자열은 brand.ts 한 곳에만 있다", () => {
    for (const [file, text] of Object.entries(sourceFiles)) {
      if (file === "/src/brand.ts") continue;
      expect(stripComments(text), file).not.toMatch(/Orbit/);
    }
  });
});

// ── 자체 호스팅 글꼴 ─────────────────────────────────────────────────────

const fontFiles = import.meta.glob("/src/styles/fonts/*.woff2", {
  query: "?url",
  import: "default",
  eager: true,
});
const license = only(
  import.meta.glob<string>("/src/styles/fonts/LICENSE.txt", {
    query: "?raw",
    import: "default",
    eager: true,
  }),
  "/src/styles/fonts/LICENSE.txt",
);
const fontReadme = only(
  import.meta.glob<string>("/src/styles/fonts/README.md", {
    query: "?raw",
    import: "default",
    eager: true,
  }),
  "/src/styles/fonts/README.md",
);

describe("자체 호스팅 글꼴", () => {
  it("fonts.css가 가리키는 파일이 모두 있고 쓰지 않는 파일은 없다", () => {
    const referenced = new Set(
      [...fontsCss.matchAll(/url\("\.\/fonts\/([^"]+\.woff2)"\)/g)].map((match) => match[1] ?? ""),
    );
    const present = new Set(Object.keys(fontFiles).map((path) => path.split("/").pop() ?? ""));
    expect(referenced.size).toBeGreaterThan(0);
    expect(present).toEqual(referenced);
  });

  it("외부 CDN을 쓰지 않고 unicode-range로 나눠 내려받는다", () => {
    expect(fontsCss).not.toMatch(/https?:\/\//);
    const faces = fontsCss.match(/@font-face/g)?.length ?? 0;
    const ranges = fontsCss.match(/unicode-range:/g)?.length ?? 0;
    expect(faces).toBeGreaterThan(0);
    expect(ranges).toBe(faces);
  });

  it("라이선스(OFL)를 함께 두고 버전이 브랜드 가이드와 같다", () => {
    expect(license).toContain("SIL Open Font License");
    const guideVersion = /`@ibm\/plex-sans-kr`\s+(\d+\.\d+\.\d+)/.exec(brandGuide)?.[1];
    expect(guideVersion).toBeDefined();
    expect(fontReadme).toContain(`**${guideVersion ?? ""}**`);
    expect(fontsCss).toContain(`IBM Plex Sans KR ${guideVersion ?? ""}`);
  });
});
