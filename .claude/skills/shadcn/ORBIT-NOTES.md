# Orbit 설치 기록
- 출처: https://github.com/shadcn-ui/ui 의 `skills/shadcn` (커밋 4233ed1caded8c38f4bc14b14afe1f93ebcaf966, 2026-11-14 설치). 라이선스 MIT(LICENSE.md는 저장소 루트 파일).
- 원본에서 바꾼 것(이 두 가지만): (1) SKILL.md의 `allowed-tools` 줄 제거, (2) 스킬을 불러올 때 `npx shadcn@latest info --json`을 자동 실행하던 `!` 블록 제거. 이유: npm에서 외부 코드를 받아 자동 실행하는 경로를 만들지 않기 위해서다. 그 외 파일은 원본 그대로다.
- Orbit 사용 범위는 `.claude/project-skills/shadcn.md`를 먼저 읽는다. Orbit은 Tailwind·Radix·shadcn CLI를 쓰지 않는다.
