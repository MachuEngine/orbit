// 선화 봉투. 오른쪽 아래에 4x4 Bayer 행렬로 만든 디더링 그림자가 옅어지며 퍼진다.
const BAYER = [
  [0, 8, 2, 10],
  [12, 4, 14, 6],
  [3, 11, 1, 9],
  [15, 7, 13, 5],
] as const;

const WIDTH = 120;
const HEIGHT = 80;
const OFFSET = 8;
const CELL = 3;
const VIEW_W = WIDTH + OFFSET + 16;
const VIEW_H = HEIGHT + OFFSET + 16;
const LEFT = 4;
const TOP = 4;

interface Cell {
  key: string;
  x: number;
  y: number;
}

const SHADOW: Cell[] = [];
const COLS = Math.ceil((WIDTH + OFFSET) / CELL);
const ROWS = Math.ceil((HEIGHT + OFFSET) / CELL);
for (let row = 0; row < ROWS; row += 1) {
  for (let col = 0; col < COLS; col += 1) {
    const x = LEFT + OFFSET + col * CELL;
    const y = TOP + OFFSET + row * CELL;
    // 봉투 본체 아래 영역은 가려지므로 오른쪽·아래로 삐져나온 부분만 그린다.
    const insideBody = x < LEFT + WIDTH && y < TOP + HEIGHT;
    if (insideBody) continue;
    // 오른쪽 아래로 갈수록 점이 드물어진다.
    const fade = 1 - (col / COLS + row / ROWS) / 2;
    const threshold = BAYER[row % 4]?.[col % 4] ?? 0;
    if (fade * 16 > threshold) SHADOW.push({ key: `${col}-${row}`, x, y });
  }
}

/** 빈 받은편지함 같은 화면에 쓰는 봉투 그림. */
export function Envelope({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
      fill="currentColor"
      aria-hidden="true"
      focusable="false"
    >
      {SHADOW.map((cell) => (
        <rect key={cell.key} x={cell.x} y={cell.y} width="1.5" height="1.5" />
      ))}
      <g fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="miter">
        <rect x={LEFT} y={TOP} width={WIDTH} height={HEIGHT} />
        <path
          d={`M${LEFT} ${TOP} L${LEFT + WIDTH / 2} ${TOP + HEIGHT * 0.58} L${LEFT + WIDTH} ${TOP}`}
        />
        <path d={`M${LEFT} ${TOP + HEIGHT} L${LEFT + WIDTH * 0.38} ${TOP + HEIGHT * 0.45}`} />
        <path
          d={`M${LEFT + WIDTH} ${TOP + HEIGHT} L${LEFT + WIDTH * 0.62} ${TOP + HEIGHT * 0.45}`}
        />
      </g>
    </svg>
  );
}
