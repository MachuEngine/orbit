# Orbit

메일·캘린더·문서를 다루는 대화형 개인 AI 비서. 웹을 먼저 개발하고, 이후 데스크톱과 다른 제공자로 확장한다.

> 현재 상태: 설계 단계. 구현 코드는 아직 없다.

## 문서

- [아키텍처 개요](docs/architecture.md)
- [ADR](docs/adr/)

세부 설계 원본은 비공개로 관리하며 이 저장소에 포함하지 않는다.

## 로컬 검사

```bash
scripts/check.sh
```

비밀정보 탐지에 [gitleaks](https://github.com/gitleaks/gitleaks)를 쓴다. 로컬에 없으면 해당 검사는 경고만 하고 건너뛰며, CI에서는 실패로 처리한다.

## 환경 변수

`.env.example`을 `.env`로 복사해 값을 채운다. `.env`는 커밋하지 않는다.
