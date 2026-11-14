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

/** tokens.json이 정의하는 모든 값을 CSS 변수 이름으로 펼친다. 다크 전용이라 모드 구분이 없다. */
function allVars(): Record<string, string> {
  const vars: Record<string, string> = {};
  const add = (prefix: string, path: string) => {
    for (const [name, value] of leaves(path)) vars[`--${prefix}-${name}`] = value;
  };
  add("color", "color");
  add("font", "font.family");
  add("font-weight", "font.weight");
  add("text", "font.size");
  add("leading", "font.leading");
  add("tracking", "font.tracking");
  add("measure", "font.measure");
  add("space", "space");
  add("shadow", "shadow");
  add("motion", "motion.duration");
  add("ease", "motion.easing");
  return vars;
}

function declarations(block: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const match of block.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    result[match[1] ?? ""] = (match[2] ?? "").replace(/\s+/g, " ").trim();
  }
  return result;
}

const lower = (vars: Record<string, string>) =>
  Object.fromEntries(Object.entries(vars).map(([key, value]) => [key, value.toLowerCase()]));

// 첫 `:root` 블록에 모든 값이 있다(중첩이 없어 `[^}]*`로 읽는다). 뒤의 reduced-motion 블록은 모션 값만 덮어쓴다.
const rootMatch = /^:root\s*\{([^}]*)\}/m.exec(tokensCss);
if (rootMatch?.[1] === undefined) throw new Error("tokens.css에서 :root 블록을 찾지 못했다");
const rootCss = declarations(rootMatch[1]);

describe("tokens.css와 tokens.json", () => {
  it("모든 값이 같다(json에 없는 변수가 css에 없고, css에 없는 값이 json에 없다)", () => {
    expect(lower(rootCss)).toEqual(lower(allVars()));
  });

  it("다크 전용이다: 라이트 모드·data-theme·prefers-color-scheme 분기가 없다", () => {
    expect(tokensCss).toMatch(/color-scheme:\s*dark;/);
    expect(tokensCss).not.toMatch(/prefers-color-scheme|data-theme|color-scheme:\s*light/);
    expect(Object.keys(tokens)).not.toContain("radius");
  });
});

// ── 대비 기준(brand-guide 8절 대비표와 같은 계산) ─────────────────────────

function lum(hex: string): number {
  const channel = (offset: number) => {
    const c = parseInt(hex.slice(offset, offset + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
}

function ratio(a: string, b: string): number {
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return ((hi ?? 0) + 0.05) / ((lo ?? 0) + 0.05);
}

/** fg를 알파 a로 bg 위에 올린 색(#RRGGBB). 종이 질감 최악 대비 계산에 쓴다. */
function mix(fg: string, a: number, bg: string): string {
  const part = (offset: number) =>
    Math.round(
      parseInt(fg.slice(offset, offset + 2), 16) * a +
        parseInt(bg.slice(offset, offset + 2), 16) * (1 - a),
    )
      .toString(16)
      .padStart(2, "0");
  return `#${part(1)}${part(3)}${part(5)}`;
}

const hex = (name: string) => {
  const value = String(at(`color.${name}`).$value);
  if (!/^#[0-9a-fA-F]{6}$/.test(value)) throw new Error(`${name}: hex 색이 아니다(${value})`);
  return value;
};

describe("색 대비 기준", () => {
  const darkBgs = ["bg", "surface", "surface-sunken"];
  const lightBgs = ["paper", "sheet", "sheet-deep", "section-sand", "section-tint"];

  it("어두운 바탕 위 글자는 4.5 이상, 테두리·포커스 링은 3 이상이다", () => {
    for (const bg of darkBgs) {
      for (const fg of [
        "text",
        "text-muted",
        "accent",
        "info",
        "warning",
        "danger",
        "success",
        "error",
      ]) {
        expect(ratio(hex(fg), hex(bg)), `${fg} / ${bg}`).toBeGreaterThanOrEqual(4.5);
      }
      for (const fg of ["border", "focus"]) {
        expect(ratio(hex(fg), hex(bg)), `${fg} / ${bg}`).toBeGreaterThanOrEqual(3);
      }
    }
  });

  it("밝은 바탕 위 글자는 4.5 이상, ink(링·선)는 3 이상이다", () => {
    for (const bg of lightBgs) {
      for (const fg of [
        "on-paper",
        "on-paper-muted",
        "accent-on-paper",
        "paper-info",
        "paper-warning",
        "paper-danger",
        "paper-success",
        "paper-error",
      ]) {
        expect(ratio(hex(fg), hex(bg)), `${fg} / ${bg}`).toBeGreaterThanOrEqual(4.5);
      }
      expect(ratio(hex("ink"), hex(bg)), `ink / ${bg}`).toBeGreaterThanOrEqual(3);
    }
  });

  it("종이 질감(최대 4% 어두워짐)을 얹은 최악 바탕에서도 본문·보조·코발트 글자가 4.5 이상이다", () => {
    for (const bg of lightBgs) {
      const worst = mix(hex("ink"), 0.04, hex(bg));
      for (const fg of ["on-paper", "on-paper-muted", "accent-on-paper"]) {
        expect(ratio(hex(fg), worst), `${fg} / ${bg}+질감`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("주요 버튼은 ink 바탕 + paper 글자(밝은 서피스), paper 바탕 + ink 글자(어두운 바탕)이고 둘 다 4.5 이상이다", () => {
    expect(ratio(hex("paper"), hex("ink"))).toBeGreaterThanOrEqual(4.5);
    // 알림 배지(ink 바탕 + paper 글자 + paper 테두리)는 종이 버튼·시트·사이드바 위에서 경계가 3 이상이다.
    for (const bg of ["paper", "sheet", "sheet-deep"]) {
      expect(ratio(hex("ink"), hex(bg)), `ink / ${bg}`).toBeGreaterThanOrEqual(3);
    }
    // 코발트 버튼 바탕 토큰은 더 쓰지 않는다.
    expect(allVars()).not.toHaveProperty("--color-accent-fill");
    expect(allVars()).not.toHaveProperty("--color-on-accent");
  });

  it("sheet-deep은 채팅 바탕(sheet)보다 어둡고 on-paper 대비 4.5 이상, 현재 항목 띠(paper)와는 구분된다", () => {
    expect(ratio(hex("sheet-deep"), hex("ink"))).toBeLessThan(ratio(hex("sheet"), hex("ink")));
    expect(ratio(hex("paper"), hex("sheet-deep"))).toBeGreaterThan(1.3);
    expect(ratio(hex("accent-on-paper"), hex("paper"))).toBeGreaterThanOrEqual(3);
    expect(ratio(hex("accent-on-paper"), hex("sheet-deep"))).toBeGreaterThanOrEqual(3);
  });

  it("어두운 바탕용 accent는 밝은 바탕에서 대비 미달이라 글자에 쓰면 안 된다(규칙의 근거)", () => {
    for (const bg of lightBgs) {
      expect(ratio(hex("accent"), hex(bg)), bg).toBeLessThan(3);
    }
  });

  it("info는 메인 코발트(accent)와 색상이 구분된다: 파랑 계열(R보다 B가 큰 정도)이 아니라 청록 쪽이다", () => {
    const channels = (value: string) => [1, 3, 5].map((i) => parseInt(value.slice(i, i + 2), 16));
    const [ir, , ib] = channels(hex("info"));
    const [ar, , ab] = channels(hex("accent"));
    // accent는 빨강 성분이 높은 코발트 계열(R이 G보다 낮지만 B와 큰 차), info는 R이 가장 낮고 G가 B에 가깝다.
    expect((ab ?? 0) - (ar ?? 0)).toBeGreaterThan(60);
    expect(ir ?? 255).toBeLessThan(ar ?? 0);
    const [, ig] = channels(hex("info"));
    expect(Math.abs((ig ?? 0) - (ib ?? 0))).toBeLessThan(40);
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
  it("스타일 파일이 있다(공용 컴포넌트와 스타일가이드 포함)", () => {
    expect(Object.keys(cssFiles).length).toBeGreaterThanOrEqual(4);
    expect(Object.keys(cssFiles)).toContain("/src/styles/components.css");
    expect(Object.keys(cssFiles)).toContain("/src/styleguide/styleguide.css");
    expect(Object.keys(sourceFiles)).toContain("/src/styleguide/Styleguide.tsx");
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

  it("쓰는 글꼴 굵기는 해당 글꼴이 자체 호스팅한 굵기 안에 있다", () => {
    const vars = allVars();
    // 글꼴 이름별로 fonts.css가 호스팅한 굵기
    const shipped = new Map<string, Set<number>>();
    for (const face of fontsCss.matchAll(/@font-face\s*\{([^}]*)\}/g)) {
      const family = /font-family:\s*"([^"]+)"/.exec(face[1] ?? "")?.[1];
      const weight = Number(/font-weight:\s*(\d+);/.exec(face[1] ?? "")?.[1]);
      if (family === undefined) continue;
      shipped.set(family, (shipped.get(family) ?? new Set<number>()).add(weight));
    }
    const everything = new Set([...shipped.values()].flatMap((weights) => [...weights]));
    const familyOf = (token: string) => /"([^"]+)"/.exec(vars[token] ?? "")?.[1] ?? "";
    let checked = 0;
    for (const [file, text] of Object.entries(cssFiles)) {
      // 한계: 글꼴을 같은 규칙에서 지정하지 않으면 상속받는 글꼴을 정규식으로 알 수 없어 전체 호스팅 굵기와만 비교한다.
      // 반대로 `font-family: var(--font-mono|pixel)`처럼 같은 규칙에서 글꼴을 정하면 그 글꼴이 호스팅한 굵기만
      // 허용하므로, 호스팅하지 않은 굵기 토큰(예: pixel에 semibold)은 여기서 실패한다.
      // 규칙 하나(`선택자 { 선언 }`)씩 읽는다. @media 안의 규칙도 안쪽 블록으로 잡힌다.
      for (const rule of stripComments(text).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
        const body = rule[2] ?? "";
        const weightToken = /font-weight:\s*var\((--font-weight-[\w-]+)\)/.exec(body)?.[1];
        if (weightToken === undefined) continue;
        const weight = Number(vars[weightToken]);
        const familyToken = /font-family:\s*var\((--font-[\w-]+)\)/.exec(body)?.[1];
        // 글꼴을 같은 규칙에서 정하면 그 글꼴, 아니면 상속받으므로 호스팅한 전체 굵기 안에 있어야 한다.
        const allowed =
          familyToken === undefined
            ? everything
            : (shipped.get(familyOf(familyToken)) ?? new Set());
        expect(allowed.has(weight), `${file} ${(rule[1] ?? "").trim()} ${weightToken}`).toBe(true);
        checked += 1;
      }
    }
    expect(checked).toBeGreaterThan(0);
    expect(shipped.get("Galmuri11")).toEqual(new Set([400]));
  });

  // 밝은 서피스 목록은 base.css 한 곳(--focus-ring·--link-*·--tone-* 변수를 바꾸는 규칙)이고,
  // components.css의 --row-line 규칙 두 개(sheet 선·ink 선)를 합치면 같은 목록이 된다.
  const classNames = (list: string) =>
    [...list.matchAll(/\.([\w-]+)/g)].map((name) => name[1] ?? "");
  const propNames = (block: string) =>
    [...block.matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1] ?? "").filter((n) => n !== "--row-line");

  function lightLists() {
    const base = stripComments(only(cssFiles, "/src/styles/base.css"));
    const light = /:is\(([^)]*)\)\s*\{([^}]*--focus-ring[^}]*)\}/.exec(base);
    const island = /:root,\s*\.surface-dark\s*\{([^}]*)\}/.exec(base);
    const components = stripComments(only(cssFiles, "/src/styles/components.css"));
    const rowSheet = /:is\(([^)]*)\)\s*\{[^}]*--row-line:\s*var\(--color-sheet\)/.exec(components);
    const rowInk = /:is\(([^)]*)\)\s*\{[^}]*--row-line:\s*var\(--color-ink\)/.exec(components);
    return { base, components, light, island, rowSheet, rowInk };
  }

  it("밝은 서피스 목록은 base.css 한 곳에 있고 --row-line 목록(sheet·ink)과 합치면 같다", () => {
    const { base, components, light, island, rowSheet, rowInk } = lightLists();
    expect(light?.[1]).toBeDefined();
    expect(island?.[1]).toBeDefined();
    const listed = classNames(light?.[1] ?? "");
    expect(listed.length).toBeGreaterThan(0);
    // 목록은 한 곳뿐이다(링크·포커스·의미 색 규칙이 목록을 따로 들고 있지 않다).
    expect(base.match(/:is\(/g)).toHaveLength(1);
    const rows = [...classNames(rowSheet?.[1] ?? ""), ...classNames(rowInk?.[1] ?? "")];
    expect(new Set(rows)).toEqual(new Set(listed));
    expect(rows.length).toBe(new Set(rows).size);
    // 시트·모래·틴트 바탕은 ink 선이다(sheet 선은 시트 위에서 사라진다).
    expect(classNames(rowInk?.[1] ?? "")).toEqual(
      expect.arrayContaining(["section-sheet", "section-sand", "section-tint", "chat"]),
    );
    expect(classNames(rowSheet?.[1] ?? "")).not.toContain("section-sheet");
    expect(classNames(rowSheet?.[1] ?? "")).not.toContain("chat");
    // 변수를 쓰는 규칙
    expect(base).toMatch(/:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--focus-ring\)/);
    expect(base).toMatch(/\ba\s*\{[^}]*color:\s*var\(--link-color\)/);
    expect(components).toMatch(
      /:root,\s*\.surface-dark\s*\{[^}]*--row-line:\s*var\(--color-divider\)/,
    );
  });

  it("어두운 섬(.surface-dark)은 밝은 서피스가 바꾸는 변수를 모두 어두운 바탕 값으로 되돌린다", () => {
    const { light, island, components } = lightLists();
    const changed = propNames(light?.[2] ?? "");
    expect(changed.length).toBeGreaterThan(0);
    expect(new Set(propNames(island?.[1] ?? ""))).toEqual(new Set(changed));
    const values = declarations(island?.[1] ?? "");
    expect(values["--focus-ring"]).toBe("var(--color-focus)");
    expect(values["--link-color"]).toBe("var(--color-accent)");
    expect(values["--primary-bg"]).toBe("var(--color-paper)");
    expect(values["--primary-fg"]).toBe("var(--color-ink)");
    expect(values["--primary-shadow"]).toBe("none");
    for (const tone of ["info", "success", "warning", "danger", "error"]) {
      expect(values[`--tone-${tone}`]).toBe(`var(--color-${tone})`);
    }
    // --row-line도 같은 섬 규칙에서 divider로 되돌린다.
    expect(components).toMatch(
      /:root,\s*\.surface-dark\s*\{[^}]*--row-line:\s*var\(--color-divider\)/,
    );
  });

  it("밝은 바탕을 가진 컨테이너 클래스는 모두 밝은 서피스 목록에 들어 있다", () => {
    const { light } = lightLists();
    const listed = classNames(light?.[1] ?? "");
    const lightBg =
      /background:\s*var\(--color-(paper|sheet|sheet-deep|section-sand|section-tint)\)/;
    // 컨트롤(버튼·진행 막대)과 개발 전용 화면(스타일가이드·미리보기)은 포커스를 받는 서피스가 아니라 제외한다.
    const skipFile = (file: string) =>
      file === "/src/styleguide/styleguide.css" || file === "/src/preview/preview.css";
    const skipName = (name: string) => name.startsWith("btn") || name.startsWith("progress");
    const found = new Set<string>();
    for (const [file, text] of Object.entries(cssFiles)) {
      if (skipFile(file)) continue;
      for (const rule of stripComments(text).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
        if (!lightBg.test(rule[2] ?? "")) continue;
        for (const selector of (rule[1] ?? "").split(",")) {
          const name = /^\s*\.([\w-]+)\s*$/.exec(selector)?.[1];
          if (name !== undefined && !skipName(name)) found.add(name);
        }
      }
    }
    for (const name of [
      "approval",
      "conn-confirm",
      "idx-progress",
      "chat",
      "conversation-panel",
      "conn",
      "login",
      "retro-body",
      "paper",
    ]) {
      expect(found, name).toContain(name);
    }
    for (const name of found) expect(listed, name).toContain(name);
  });

  it("밝은 서피스 안에 남는 어두운 요소는 .surface-dark를 붙인 어두운 섬이다", () => {
    const sources = import.meta.glob<string>(["/src/notifications/NotificationPanel.tsx"], {
      query: "?raw",
      import: "default",
      eager: true,
    });
    // 어두운 섬 클래스(CSS가 어두운 바탕을 그리는 요소)와 그 요소를 그리는 tsx
    const islands: [string, string, string][] = [
      [
        "/src/notifications/notifications.css",
        ".notify-panel",
        "/src/notifications/NotificationPanel.tsx",
      ],
    ];
    const dark = /background:\s*var\(--color-(surface|surface-sunken|bg)\)/;
    for (const [css, selector, tsx] of islands) {
      const code = stripComments(only(cssFiles, css));
      const hit = [...code.matchAll(/([^{}]+)\{([^{}]*)\}/g)].some(
        (rule) =>
          dark.test(rule[2] ?? "") &&
          (rule[1] ?? "").split(",").some((sel) => sel.trim() === selector),
      );
      expect(hit, `${selector} 규칙이 어두운 바탕이다`).toBe(true);
      expect(only(sources, tsx), `${tsx}에 surface-dark가 있다`).toContain("surface-dark");
    }
    // 채팅 영역 css에서 어두운 바탕을 그리는 클래스는 위 목록에 있는 것뿐이다.
    const known = new Set<string>();
    for (const rule of stripComments(only(cssFiles, "/src/chat/chat.css")).matchAll(
      /([^{}]+)\{([^{}]*)\}/g,
    )) {
      if (!dark.test(rule[2] ?? "")) continue;
      for (const selector of (rule[1] ?? "").split(",")) {
        expect(known, selector.trim()).toContain(selector.trim());
      }
    }
  });

  it("밝은 바탕 안의 의미 색은 종이용(paper-*) 색 변수로 바뀐다", () => {
    const { base, light } = lightLists();
    const block = light?.[2] ?? "";
    for (const tone of ["info", "success", "warning", "danger", "error"]) {
      expect(block, tone).toMatch(new RegExp(`--tone-${tone}:\\s*var\\(--color-paper-${tone}\\)`));
      expect(base, tone).toMatch(new RegExp(`\\.tone-${tone}\\s*\\{[^}]*var\\(--tone-${tone}\\)`));
    }
  });

  it("주요 버튼은 밝은 서피스에서 ink 바탕·paper 글자·각진 그림자, 어두운 바탕에서 paper 바탕·ink 글자이고 .btn-ink는 그림자가 없다", () => {
    const { base, light } = lightLists();
    const values = declarations(light?.[2] ?? "");
    expect(values["--primary-bg"]).toBe("var(--color-ink)");
    expect(values["--primary-fg"]).toBe("var(--color-paper)");
    expect(values["--primary-shadow"]).toBe("var(--shadow-hard)");
    expect(base).toMatch(
      /\.btn-primary\s*\{[^}]*background:\s*var\(--primary-bg\);[^}]*box-shadow:\s*var\(--primary-shadow\);[^}]*color:\s*var\(--primary-fg\)/,
    );
    expect(/\.btn-ink\s*\{([^}]*)\}/.exec(base)?.[1]).not.toMatch(/box-shadow/);
    expect(base).not.toMatch(/accent-fill|--btn-edge/);
    // 알림 배지: ink 바탕 + paper 글자
    const badge = /\.notify-badge\s*\{([^}]*)\}/.exec(
      stripComments(only(cssFiles, "/src/notifications/notifications.css")),
    )?.[1];
    expect(badge).toMatch(/background:\s*var\(--color-ink\)/);
    expect(badge).toMatch(/color:\s*var\(--color-paper\)/);
  });

  it("스타일가이드 색 견본이 tokens.json의 색 토큰과 정확히 같다", () => {
    const css = stripComments(only(cssFiles, "/src/styleguide/styleguide.css"));
    const swatches = [...css.matchAll(/\.sg-swatch\[data-token="([^"]+)"\]/g)].map(
      (m) => m[1] ?? "",
    );
    expect(new Set(swatches).size).toBe(swatches.length);
    expect(new Set(swatches)).toEqual(new Set(leaves("color").map(([name]) => name)));
  });

  it("알림 패널: 포커스를 받는 .notify-panel이 스크롤 컨테이너이고 도트 그림자는 바깥 틀이 그린다", () => {
    const css = stripComments(only(cssFiles, "/src/notifications/notifications.css"));
    const body = (selector: string) =>
      new RegExp(`(?:^|\\})\\s*${selector.replace(/[.:]/g, "\\$&")}\\s*\\{([^}]*)\\}`).exec(
        css,
      )?.[1] ?? "";
    expect(body(".notify-panel")).toMatch(/overflow-y:\s*auto/);
    expect(body(".notify-body")).not.toMatch(/overflow/);
    expect(body(".notify-frame::before")).toMatch(/conic-gradient/);
    expect(css).not.toMatch(/\.notify-panel::before/);
  });

  it("알림 패널: 스크롤 컨테이너의 scroll-padding-top이 sticky 머리 높이 이상이다", () => {
    const css = stripComments(only(cssFiles, "/src/notifications/notifications.css"));
    const panel = /(?:^|\})\s*\.notify-panel\s*\{([^}]*)\}/.exec(css)?.[1] ?? "";
    expect(panel).toMatch(/scroll-padding-top:\s*calc\(var\(--space-6\) \+ var\(--space-4\)\)/);
    // 머리 높이: 버튼 최소 높이 space-6 + 위아래 패딩 space-1 둘 + 테두리 1px = 2.5rem + 1px 안쪽이다.
    expect(css).toMatch(/\.notify-head\s*\{[^}]*position:\s*sticky/);
    expect(css).toMatch(/\.notify-head \.btn\s*\{[^}]*min-height:\s*var\(--space-6\)/);
  });

  it("대화 드로어: 스크롤하는 패널이 아니라 바깥 틀이 각진 그림자를 그린다", () => {
    const css = stripComments(only(cssFiles, "/src/conversations/conversations.css"));
    expect(css).toMatch(/\.conversation-drawer-frame\s*\{[^}]*box-shadow:\s*var\(--shadow-hard\)/);
    expect(css).not.toMatch(/\.conversation-(panel|drawer)::before/);
    expect(css).not.toMatch(/\.conversation-(panel|drawer)\s*\{[^}]*box-shadow/);
  });

  it("사이드바는 sheet-deep 밝은 서피스이고 글자·선·로딩 점이 밝은 바탕용 색이다", () => {
    const css = stripComments(only(cssFiles, "/src/conversations/conversations.css"));
    const panel = /\.conversation-panel\s*\{([^}]*)\}/.exec(css)?.[1] ?? "";
    expect(panel).toMatch(/background:\s*var\(--color-sheet-deep\)/);
    expect(panel).toMatch(/border-right:\s*1px solid var\(--color-ink\)/);
    expect(css).not.toMatch(/--color-(surface|text|text-muted|cell|accent)\b(?!-)/);
    expect(css).toMatch(/\.conversation-loading \.loading-dots span\s*\{[^}]*var\(--color-ink\)/);
    expect(css).toMatch(/\.conversation-drawer-frame\s*\{[^}]*box-shadow:\s*var\(--shadow-hard\)/);
    const { light } = lightLists();
    expect(classNames(light?.[1] ?? "")).toContain("conversation-panel");
  });

  it("사이드바 현재 항목: paper 띠 + 코발트 선이고 ink 포커스 링은 음수 outline-offset으로 띠 안쪽에 그린다", () => {
    const css = stripComments(only(cssFiles, "/src/conversations/conversations.css"));
    const base = /\.conversation-item\[aria-current="true"\]\s*\{([^}]*)\}/.exec(css)?.[1] ?? "";
    expect(base).toMatch(/--focus-ring:\s*var\(--color-ink\)/);
    expect(base).toMatch(/background:\s*var\(--color-paper\)/);
    expect(base).toMatch(/border-left:\s*2px solid var\(--color-accent-on-paper\)/);
    const focus =
      /\.conversation-item\[aria-current="true"\]:focus-visible\s*\{([^}]*)\}/.exec(css)?.[1] ?? "";
    expect(focus).toMatch(/outline-offset:\s*-\d+px/);
  });

  it("승인 카드는 시트 위의 hard 창이다: ink 테두리, 검정 각진 그림자, 자신의 포커스 링도 ink다", () => {
    const approval = stripComments(only(cssFiles, "/src/approvals/approval.css"));
    const card = /(?:^|\})\s*\.approval\s*\{([^}]*)\}/.exec(approval)?.[1] ?? "";
    expect(card).toMatch(/border:\s*1px solid var\(--color-ink\)/);
    expect(card).toMatch(/box-shadow:\s*var\(--shadow-hard\)/);
    // 흰 링으로 되돌리는 규칙이 없다. 카드는 밝은 서피스 목록에 있어 --focus-ring이 ink다.
    expect(approval).not.toMatch(/\.approval(\.retro)?:focus-visible/);
    expect(approval).not.toMatch(/--color-focus/);
    const { light } = lightLists();
    expect(classNames(light?.[1] ?? "")).toContain("approval");
    expect(light?.[2]).toMatch(/--focus-ring:\s*var\(--color-ink\)/);
  });

  it("계정 연결 확인 상자·진행 창은 시트 위 hard 변형(ink 테두리, 검정 단색 그림자)이고 흰 포커스 링을 쓰지 않는다", () => {
    const css = stripComments(only(cssFiles, "/src/connections/connections.css"));
    // 시트 위라 base.css 밝은 서피스 목록의 ink 링을 그대로 쓴다(흰 링으로 되돌리는 규칙이 없다).
    expect(css).not.toMatch(/\.conn-confirm(\.retro)?:focus-visible/);
    expect(css).not.toMatch(/--color-focus/);
    const box = /\.conn-confirm,\s*\.idx-progress\s*\{([^}]*)\}/.exec(css)?.[1] ?? "";
    expect(box).toMatch(/border:\s*1px solid var\(--color-ink\)/);
    expect(box).toMatch(/box-shadow:\s*var\(--shadow-hard\)/);
    // 도트 그림자(.retro-dither)는 다크 바탕용이라 시트 위 두 창에는 쓰지 않는다.
    for (const file of [
      "/src/connections/ConnectionsScreen.tsx",
      "/src/connections/MailIndexSection.tsx",
    ]) {
      expect(only(sourceFiles, file), file).not.toContain("retro-dither");
    }
    // 표 행 구분선은 --row-line이다.
    const components = stripComments(only(cssFiles, "/src/styles/components.css"));
    expect(components).toMatch(
      /\.table-rows\s*>\s*li\s*\{[^}]*border-top:\s*1px solid var\(--row-line\)/,
    );
  });

  it("계정 연결·로그인 화면은 시트 바탕 밝은 서피스이고 카드는 종이 바탕 + ink 테두리다", () => {
    const { light, rowInk } = lightLists();
    for (const name of ["conn", "conn-card", "login", "login-card"]) {
      expect(classNames(light?.[1] ?? ""), name).toContain(name);
      expect(classNames(rowInk?.[1] ?? ""), name).toContain(name);
    }
    const conn = stripComments(only(cssFiles, "/src/connections/connections.css"));
    const login = stripComments(only(cssFiles, "/src/session/login.css"));
    expect(/\.conn\s*\{([^}]*)\}/.exec(conn)?.[1]).toMatch(/background:\s*var\(--color-sheet\)/);
    expect(/\.login\s*\{([^}]*)\}/.exec(login)?.[1]).toMatch(/background:\s*var\(--color-sheet\)/);
    const card = /\.conn-card\s*\{([^}]*)\}/.exec(conn)?.[1] ?? "";
    expect(card).toMatch(/background:\s*var\(--color-paper\)/);
    expect(card).toMatch(/border:\s*1px solid var\(--color-ink\)/);
    const loginCard = /\.login-card\s*\{([^}]*)\}/.exec(login)?.[1] ?? "";
    expect(loginCard).toMatch(/border:\s*1px solid var\(--color-ink\)/);
    expect(loginCard).toMatch(/box-shadow:\s*var\(--shadow-hard\)/);
    // 시트·종이 위에서 대비 미달인 어두운 바탕용 accent를 글자·아이콘·밑줄로 쓰지 않는다.
    // 밝은 바탕에서는 accent-on-paper를 쓰고, 버튼 바탕은 .btn-primary(ink)만이다.
    for (const css of [conn, login]) expect(css).not.toMatch(/var\(--color-accent\)/);
    for (const file of ["/src/chat/chat.css", "/src/approvals/approval.css"]) {
      expect(stripComments(only(cssFiles, file)), file).not.toMatch(/var\(--color-accent\)/);
    }
  });

  it("밝은 서피스 안의 목록 구분선은 sheet 장식선(시트 위는 ink)이고 어두운 바탕에서는 divider다", () => {
    const { rowSheet, rowInk, components } = lightLists();
    expect(components).toMatch(
      /:root,\s*\.surface-dark\s*\{[^}]*--row-line:\s*var\(--color-divider\)/,
    );
    for (const name of ["paper", "retro-body", "approval", "conn-confirm", "idx-progress"]) {
      expect(classNames(rowSheet?.[1] ?? ""), name).toContain(name);
    }
    expect(classNames(rowInk?.[1] ?? "")).toContain("chat");
    const approval = stripComments(only(cssFiles, "/src/approvals/approval.css"));
    expect(approval).toMatch(/\.approval-row\s*\{[^}]*border-top:\s*1px solid var\(--row-line\)/);
    expect(approval).not.toMatch(/--color-divider/);
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

  it("모서리는 직각이다: border-radius는 0만 쓴다", () => {
    for (const [file, text] of Object.entries(cssFiles)) {
      for (const match of stripComments(text).matchAll(/border(?:-[a-z]+)*-radius:\s*([^;]+);/g)) {
        expect(match[1]?.trim(), file).toMatch(/^0(px)?$/);
      }
    }
  });

  it("그림자는 각진 것만 쓴다: --shadow-hard, none, 또는 blur가 0인 그림자", () => {
    const length = "-?[\\d.]+(?:px|rem)?";
    const sharp = new RegExp(`^(?:inset\\s+)?${length}\\s+${length}\\s+0(?:px|rem)?(?:\\s|$)`);
    for (const [file, text] of Object.entries(cssFiles)) {
      for (const match of stripComments(text).matchAll(/box-shadow:\s*([^;]+);/g)) {
        const value = (match[1] ?? "").trim();
        // --primary-shadow는 base.css에서 none 또는 --shadow-hard로만 정해진다(주요 버튼 테스트가 확인한다).
        if (["var(--shadow-hard)", "var(--primary-shadow)", "none"].includes(value)) continue;
        // 괄호 밖의 쉼표로 겹친 그림자를 나눈다.
        for (const layer of value.split(/,(?![^(]*\))/)) {
          expect(layer.trim(), file).toMatch(sharp);
        }
      }
      expect(stripComments(text), file).not.toMatch(/text-shadow|drop-shadow/);
    }
  });

  it("영문 대문자 변환 라벨을 쓰지 않는다", () => {
    for (const [file, text] of Object.entries(cssFiles)) {
      expect(stripComments(text), file).not.toMatch(/text-transform:\s*uppercase/);
    }
  });

  it("SVG data URI를 쓰지 않는다", () => {
    for (const [file, text] of Object.entries(cssFiles)) {
      expect(stripComments(text), file).not.toMatch(/data:image/);
    }
  });
});

// ── 자체 호스팅 글꼴 ─────────────────────────────────────────────────────

const FONT_PACKAGES = [
  { folder: "pretendard", pkg: "pretendard" },
  { folder: "inter-tight", pkg: "@fontsource/inter-tight" },
  { folder: "jetbrains-mono", pkg: "@fontsource/jetbrains-mono" },
  { folder: "galmuri", pkg: "galmuri" },
] as const;

const fontFiles = import.meta.glob("/src/styles/fonts/*/*.woff2", {
  query: "?url",
  import: "default",
  eager: true,
});
const fontTexts = import.meta.glob<string>("/src/styles/fonts/*/{LICENSE,LICENSE.txt,README.md}", {
  query: "?raw",
  import: "default",
  eager: true,
});

/** brand-guide.md 8절(리디자인)의 글꼴 표에서 패키지 버전을 읽는다. 표 셀 구분자를 허용한다. */
function guideVersion(pkg: string): string | undefined {
  const section8 = brandGuide.slice(brandGuide.indexOf("## 8."));
  const escaped = pkg.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");
  return new RegExp(`\`${escaped}\`\\s*\\|?\\s*(\\d+\\.\\d+\\.\\d+)`).exec(section8)?.[1];
}

describe.each(FONT_PACKAGES)("자체 호스팅 글꼴 $pkg", ({ folder, pkg }) => {
  const prefix = `/src/styles/fonts/${folder}/`;
  const present = Object.keys(fontFiles)
    .filter((path) => path.startsWith(prefix))
    .map((path) => path.slice(prefix.length));
  const referenced = [
    ...fontsCss.matchAll(new RegExp(`url\\("\\./fonts/${folder}/([^"]+\\.woff2)"\\)`, "g")),
  ].map((match) => match[1] ?? "");

  it("fonts.css가 가리키는 파일이 모두 있고 쓰지 않는 파일은 없다", () => {
    expect(referenced.length).toBeGreaterThan(0);
    expect(new Set(present)).toEqual(new Set(referenced));
    expect(referenced.length).toBe(new Set(referenced).size);
  });

  it("라이선스(OFL)를 함께 두고 버전이 브랜드 가이드와 같다", () => {
    const license = fontTexts[`${prefix}LICENSE`] ?? fontTexts[`${prefix}LICENSE.txt`];
    expect(license).toBeDefined();
    expect(license).toMatch(/(SIL )?Open Font License/);
    const version = guideVersion(pkg);
    expect(version, `${pkg} 버전을 brand-guide.md 8절에서 찾지 못했다`).toBeDefined();
    expect(fontTexts[`${prefix}README.md`]).toContain(`**${version ?? ""}**`);
    expect(fontsCss).toContain(`${pkg} ${version ?? ""}`);
  });
});

describe("자체 호스팅 글꼴 공통", () => {
  it("외부 CDN을 쓰지 않고 모든 face가 font-display와 unicode-range를 가진다", () => {
    expect(fontsCss).not.toMatch(/https?:\/\//);
    const faces = fontsCss.match(/@font-face/g)?.length ?? 0;
    expect(faces).toBeGreaterThan(0);
    expect(fontsCss.match(/unicode-range:/g)?.length ?? 0).toBe(faces);
    expect(fontsCss.match(/font-display:\s*swap;/g)?.length ?? 0).toBe(faces);
  });

  it("폴더 밖에 떨어진 글꼴 파일이 없다", () => {
    const total = Object.keys(fontFiles).length;
    const inFolders = FONT_PACKAGES.reduce(
      (sum, { folder }) =>
        sum +
        Object.keys(fontFiles).filter((p) => p.startsWith(`/src/styles/fonts/${folder}/`)).length,
      0,
    );
    expect(inFolders).toBe(total);
  });

  it("토큰의 글꼴 이름이 호스팅한 글꼴과 맞는다", () => {
    const vars = allVars();
    for (const token of ["--font-body", "--font-display", "--font-mono", "--font-pixel"]) {
      const first = /^"([^"]+)"/.exec(vars[token] ?? "")?.[1];
      expect(fontsCss, token).toContain(`font-family: "${first ?? ""}"`);
    }
  });

  it("종이 질감: 공용 클래스가 토큰 색만 쓰고 대상 컨테이너에 붙어 있다", () => {
    const components = stripComments(only(cssFiles, "/src/styles/components.css"));
    const rule = /\.paper-texture\.paper-texture\s*\{([^}]*)\}/.exec(components)?.[1];
    expect(rule).toBeDefined();
    const body = rule ?? "";
    expect(body).not.toMatch(/#[0-9a-fA-F]{3,8}\b|\b(rgb|rgba|hsl|hsla)\(|url\(|data:|filter/);
    expect(body).toMatch(/color-mix\(in srgb, var\(--color-ink\)/);
    // 레이어는 3개 이하다(background-size 항목 수로 센다).
    expect(body).toMatch(/background-size:\s*7px 7px,\s*11px 11px,\s*auto;/);
    expect(body).not.toMatch(/background-attachment/);
    const tsx = (name: string) =>
      Object.entries(sourceFiles).find(([file]) => file.endsWith(name))?.[1] ?? "";
    expect(tsx("/chat/ChatScreen.tsx")).toMatch(/className="chat paper-texture"/);
    expect(tsx("/connections/ConnectionsScreen.tsx")).toMatch(/className="conn paper-texture"/);
    expect(tsx("/connections/ConnectionsScreen.tsx")).toMatch(/conn-card[^"]*paper-texture/);
    expect(tsx("/session/LoginScreen.tsx")).toMatch(/className="login paper-texture"/);
    expect(tsx("/session/LoginScreen.tsx")).toMatch(/className="login-card paper-texture"/);
    expect(tsx("/ui/RetroWindow.tsx")).toMatch(/className="retro-body paper-texture"/);
    // 사이드바·알림 패널·타이틀바에는 붙이지 않는다.
    expect(tsx("/notifications/NotificationPanel.tsx")).not.toContain("paper-texture");
    expect(tsx("/conversations/ConversationPanel.tsx")).not.toContain("paper-texture");
    // 스크롤 영역은 고정 배경이다.
    expect(stripComments(only(cssFiles, "/src/chat/chat.css"))).not.toMatch(/attachment:\s*local/);
  });
});
