import { defineConfig } from "vitest/config";

// Electron을 실제로 띄우는 스모크 테스트. 단위 테스트와 따로 돌린다(`npm run test:smoke`).
export default defineConfig({
  test: {
    environment: "node",
    include: ["smoke/**/*.test.ts"],
    testTimeout: 60_000,
    hookTimeout: 60_000,
    fileParallelism: false,
  },
});
