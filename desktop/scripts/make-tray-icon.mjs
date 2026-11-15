// 메뉴 막대 아이콘(템플릿 이미지)을 만든다. docs/brand/logo/mark-gate.svg의 도형을 그대로 쓴다.
// 템플릿 이미지는 검정 + 투명도만 쓴다. macOS가 메뉴 막대 색(밝음·어두움)에 맞춰 칠한다.
// 사용법: node scripts/make-tray-icon.mjs  (assets/iconTemplate.png, iconTemplate@2x.png 를 다시 만든다)
import { deflateSync } from "node:zlib";
import { writeFileSync } from "node:fs";

// mark-gate.svg: <path d="M6 6 H58 V22 H48 V16 H16 V48 H48 V42 H58 V58 H6 Z"/> + <circle cx="53" cy="32" r="6"/>
const POLYGON = [
  [6, 6],
  [58, 6],
  [58, 22],
  [48, 22],
  [48, 16],
  [16, 16],
  [16, 48],
  [48, 48],
  [48, 42],
  [58, 42],
  [58, 58],
  [6, 58],
];
const CIRCLE = { cx: 53, cy: 32, r: 6 };
const VIEW = { min: 4, max: 60 }; // 여백을 조금 남기고 자른다

function inPolygon(x, y) {
  let inside = false;
  for (let i = 0, j = POLYGON.length - 1; i < POLYGON.length; j = i++) {
    const [xi, yi] = POLYGON[i];
    const [xj, yj] = POLYGON[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function covered(x, y) {
  return inPolygon(x, y) || (x - CIRCLE.cx) ** 2 + (y - CIRCLE.cy) ** 2 <= CIRCLE.r ** 2;
}

function raster(size) {
  const sub = 8; // 8x8 표본으로 가장자리를 부드럽게 한다
  const span = VIEW.max - VIEW.min;
  const rows = [];
  for (let py = 0; py < size; py++) {
    const row = Buffer.alloc(1 + size * 4); // 첫 바이트는 필터 종류(0)
    for (let px = 0; px < size; px++) {
      let hit = 0;
      for (let sy = 0; sy < sub; sy++) {
        for (let sx = 0; sx < sub; sx++) {
          const x = VIEW.min + ((px + (sx + 0.5) / sub) / size) * span;
          const y = VIEW.min + ((py + (sy + 0.5) / sub) / size) * span;
          if (covered(x, y)) hit++;
        }
      }
      row[1 + px * 4 + 3] = Math.round((hit / (sub * sub)) * 255); // RGB는 0(검정), 알파만 채운다
    }
    rows.push(row);
  }
  return rows;
}

function crc32(buf) {
  let c;
  let crc = 0xffffffff;
  for (const byte of buf) {
    c = (crc ^ byte) & 0xff;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    crc = (crc >>> 8) ^ c;
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const head = Buffer.alloc(8);
  head.writeUInt32BE(data.length, 0);
  head.write(type, 4, "ascii");
  const tail = Buffer.alloc(4);
  tail.writeUInt32BE(crc32(Buffer.concat([head.subarray(4), data])), 0);
  return Buffer.concat([head, data, tail]);
}

function png(size) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // 비트 깊이
  ihdr[9] = 6; // RGBA
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(Buffer.concat(raster(size)))),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

writeFileSync(new URL("../assets/iconTemplate.png", import.meta.url), png(18));
writeFileSync(new URL("../assets/iconTemplate@2x.png", import.meta.url), png(36));
