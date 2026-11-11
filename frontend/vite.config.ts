import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  // 글꼴 파일이 작아도 CSS에 끼워 넣지 않고 따로 내려받게 한다(unicode-range 분할이 의미를 가진다).
  build: { assetsInlineLimit: 0 },
  server: {
    // 브랜드 토큰·로고 원본(docs/brand)을 저장소 루트에서 직접 읽는다.
    fs: { allow: [".."] },
    // 개발 중에도 웹과 API를 같은 출처로 쓴다(ADR-0013).
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    restoreMocks: true,
    // 스타일 규칙 테스트가 CSS 원문(?raw)을 읽는다. 기본값은 CSS를 빈 문자열로 바꾼다.
    css: true,
  },
});
