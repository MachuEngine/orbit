// main·preload를 각각 한 파일(CommonJS)로 묶는다.
// sandbox: true 인 preload는 로컬 파일을 require할 수 없어서 하나로 묶어야 한다.
import { build } from "esbuild";

const common = {
  bundle: true,
  platform: "node",
  format: "cjs",
  target: "node24",
  sourcemap: true,
  external: ["electron"],
  logLevel: "info",
};

await build({ ...common, entryPoints: ["src/main/main.ts"], outfile: "dist/main.cjs" });
await build({ ...common, entryPoints: ["src/preload/preload.ts"], outfile: "dist/preload.cjs" });
