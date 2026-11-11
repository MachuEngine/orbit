import comboGateSvg from "../../../docs/brand/logo/combo-gate.svg?raw";
import { PRODUCT_NAME } from "../brand";

// 확정 로고(게이트 + 워드마크)의 도형을 원본 SVG에서 읽는다. 도형을 복사해 두지 않아 원본과 어긋나지 않는다.
const paths = [...comboGateSvg.matchAll(/<path d="([^"]+)"/g)].map((match) => match[1] ?? "");
const circle = /<circle cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"/.exec(comboGateSvg);
const viewBox = /viewBox="([^"]+)"/.exec(comboGateSvg)?.[1] ?? "0 0 160 64";

/** 게이트 마크와 워드마크. 색은 글자색(`currentColor`)을 따라 밝은·어두운 모드에서 모두 보인다. */
export function Logo({ className }: { className?: string }) {
  const [mark, ...word] = paths;
  return (
    <svg
      className={className}
      viewBox={viewBox}
      fill="currentColor"
      role="img"
      aria-label={PRODUCT_NAME}
    >
      <path d={mark} />
      {circle !== null && <circle cx={circle[1]} cy={circle[2]} r={circle[3]} />}
      {word.map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}
