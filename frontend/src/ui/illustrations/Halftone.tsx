// 하프톤 원형 그라데이션: 중심의 점이 가장 크고 바깥으로 갈수록 작아진다.
const GRID = 21; // 한 변의 점 개수(홀수여야 중심에 점이 놓인다)
const STEP = 10;
const SIZE = GRID * STEP;
const CENTER = SIZE / 2;
const RADIUS = CENTER - STEP / 2;
const MAX_DOT = STEP * 0.5;

interface Dot {
  key: string;
  x: number;
  y: number;
  r: number;
}

const DOTS: Dot[] = [];
for (let row = 0; row < GRID; row += 1) {
  for (let col = 0; col < GRID; col += 1) {
    const x = col * STEP + STEP / 2;
    const y = row * STEP + STEP / 2;
    const distance = Math.hypot(x - CENTER, y - CENTER);
    if (distance > RADIUS) continue;
    const r = MAX_DOT * (1 - distance / RADIUS);
    if (r < 0.4) continue;
    DOTS.push({ key: `${col}-${row}`, x, y, r: Math.round(r * 100) / 100 });
  }
}

/** 빈 화면과 로딩에 쓰는 장식 그림. 색은 글자색(currentColor)을 따른다. */
export function Halftone({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox={`0 0 ${SIZE} ${SIZE}`}
      fill="currentColor"
      aria-hidden="true"
      focusable="false"
    >
      {DOTS.map((dot) => (
        <circle key={dot.key} cx={dot.x} cy={dot.y} r={dot.r} />
      ))}
    </svg>
  );
}
