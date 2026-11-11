# Orbit 브랜드 가이드

> 상태: **채택**(2026-11-11, 개발자 선택): 팔레트 C 흑연, 글꼴 IBM Plex Sans KR, 로고마크 A 게이트, 워드마크 `Orbit`, 조합형 게이트 + `Orbit`. 나머지는 기각안이며 이력으로 남긴다. 결정 기록은 문서 끝에 있다.
> 대비 값은 `brand-identity` 스킬의 `lum`·`ratio` 함수로 계산했다. 표의 비율은 소수 둘째 자리에서 **내림**했고, 기준 판정은 내림 전 실제 값으로 했다.
> 눈으로 확인할 파일: `preview/palettes.html`(팔레트·글꼴 비교), `logo/preview.html`(로고 크기·배경별 비교).

## 1. 브리프

- **사용자**: 메일·캘린더·문서를 많이 다루는 한국어 사용자. 비서에게 일을 맡기되, 외부로 나가는 행동은 직접 승인한다.
- **성격 키워드** (방향 A, 통제·신뢰형, 개발자 확정): 통제할 수 있다 / 미리 보인다 / 차분하다 / 정확하다.
- **피할 기본값**: 보라·파랑 그라데이션과 AI 반짝이 아이콘, Inter 단독 사용, 검토 없이 Pretendard 고르기(고르면 이유를 남긴다), 크림색 배경 + 테라코타 강조색, 같은 모양의 둥근 카드를 격자로 늘어놓은 SaaS 대시보드, 거의 검은 배경 + 형광 강조색 하나.
- **표현해야 하는 상태**: 일반 정보(`info`), 외부 출처 경고(`warning`), 되돌릴 수 없음(`danger`), 성공(`success`), 오류(`error`). 색만으로 구분하지 않고 아이콘과 문구를 함께 쓴다(`preview/palettes.html`의 상태 목록 참고).

## 2. 팔레트 후보

세 후보 모두 아래 기준을 같은 방식으로 계산했다.

| 조합 | 기준 |
| --- | --- |
| 본문 글자(`text`, `text-muted`, `on-accent`, `warning`·`danger` 글자 등) / 배경 | 4.5 이상 |
| 입력창 테두리, 포커스 표시 등 UI 요소 / 인접 배경 | 3 이상 |

밝은·어두운 모드 각각에서 `bg`와 `surface` 두 배경 모두에 대해 계산했다. `accent`는 글자(링크)로 쓸 수 있으므로 4.5를 적용했다. 포커스 링이 `accent` 버튼에 맞닿는 경우의 대비는 계산하지 않았다(미확인). 장식용 `divider`는 정보를 전달하지 않아 기준에서 뺐다.

### A. 하버 (청록)

- **의도**: **통제할 수 있다**, **차분하다**. 청록 하나를 강조색으로 쓰고 나머지는 청회색 중립색으로 눌러 둔다. 청록은 신호 색(빨강·노랑)과 떨어져 있어 경고가 강조색에 묻히지 않는다.
- **기본 색**: 청록 `#0B6B6B`(밝은) / `#4FC3BD`(어두운), 청회색 `#F4F7F7` `#132122` `#788A8B`, 어두운 바탕 `#101617` `#172021`, 호박색 `#8A5300` / `#F2B24C`, 적색 `#B3261E` / `#FF8A80`, 녹색 `#1B7A3E` / `#5FD18A`
- **피할 기본값과 차이**: 보라·파랑 그라데이션과 형광 강조색을 쓰지 않고, 그라데이션 없이 단색 청록 하나만 쓴다. 바탕은 크림색이 아닌 청회색이다.
- **장단점**: 브랜드 색이 분명하다. 단점: 강조색(청록)과 `success`(녹색)가 색상 계열이 가까워 색각 이상 사용자에게 비슷하게 보일 수 있다. 보내기 버튼과 성공 표시를 문구와 아이콘으로 구분해야 한다.

**의미 색 매핑**

| 토큰 | 밝은 모드 | 어두운 모드 | 용도 |
| --- | --- | --- | --- |
| `bg` | #F4F7F7 | #101617 | 화면 바탕 |
| `surface` | #FFFFFF | #172021 | 카드·메시지·승인 카드 바탕 |
| `text` | #132122 | #E7EEEE | 본문 |
| `text-muted` | #4A5B5C | #A3B3B4 | 보조 설명 |
| `border` | #788A8B | #6B7F80 | 입력창·카드 테두리(UI 3:1) |
| `divider` | #D9E1E1 | #2A3637 | 장식 구분선(대비 기준 없음, 정보 전달에 쓰지 않음) |
| `accent` | #0B6B6B | #4FC3BD | 주 버튼·링크 |
| `on-accent` | #FFFFFF | #07201F | 주 버튼 위 글자 |
| `focus` | #0A5F9E | #7DD8D3 | 포커스 링 |
| `info` | #2B5F9E | #8DB8F0 | 일반 정보 |
| `warning` | #8A5300 | #F2B24C | 외부 출처 경고 |
| `danger` | #B3261E | #FF8A80 | 되돌릴 수 없음 |
| `success` | #1B7A3E | #5FD18A | 성공 |
| `error` | #B3261E | #FF8A80 | 오류. `danger`와 같은 값이며 아이콘(원 안의 X)과 문구로 구분한다 |

**대비표 (밝은 모드)** — 전체 통과

| 모드 | 전경 | 배경 | 비율 | 기준 | 결과 |
| --- | --- | --- | --- | --- | --- |
| 밝은 | `text` #132122 | `bg` #F4F7F7 | 15.36:1 | 4.5 | 통과 |
| 밝은 | `text` #132122 | `surface` #FFFFFF | 16.55:1 | 4.5 | 통과 |
| 밝은 | `text-muted` #4A5B5C | `bg` #F4F7F7 | 6.62:1 | 4.5 | 통과 |
| 밝은 | `text-muted` #4A5B5C | `surface` #FFFFFF | 7.13:1 | 4.5 | 통과 |
| 밝은 | `accent` #0B6B6B | `bg` #F4F7F7 | 5.85:1 | 4.5 | 통과 |
| 밝은 | `accent` #0B6B6B | `surface` #FFFFFF | 6.30:1 | 4.5 | 통과 |
| 밝은 | `info` #2B5F9E | `bg` #F4F7F7 | 6.02:1 | 4.5 | 통과 |
| 밝은 | `info` #2B5F9E | `surface` #FFFFFF | 6.49:1 | 4.5 | 통과 |
| 밝은 | `warning` #8A5300 | `bg` #F4F7F7 | 5.87:1 | 4.5 | 통과 |
| 밝은 | `warning` #8A5300 | `surface` #FFFFFF | 6.32:1 | 4.5 | 통과 |
| 밝은 | `danger` #B3261E | `bg` #F4F7F7 | 6.06:1 | 4.5 | 통과 |
| 밝은 | `danger` #B3261E | `surface` #FFFFFF | 6.53:1 | 4.5 | 통과 |
| 밝은 | `success` #1B7A3E | `bg` #F4F7F7 | 4.99:1 | 4.5 | 통과 |
| 밝은 | `success` #1B7A3E | `surface` #FFFFFF | 5.38:1 | 4.5 | 통과 |
| 밝은 | `border` #788A8B | `bg` #F4F7F7 | 3.35:1 | 3.0 | 통과 |
| 밝은 | `border` #788A8B | `surface` #FFFFFF | 3.61:1 | 3.0 | 통과 |
| 밝은 | `focus` #0A5F9E | `bg` #F4F7F7 | 6.20:1 | 3.0 | 통과 |
| 밝은 | `focus` #0A5F9E | `surface` #FFFFFF | 6.68:1 | 3.0 | 통과 |
| 밝은 | `on-accent` #FFFFFF | `accent` #0B6B6B | 6.30:1 | 4.5 | 통과 |

**대비표 (어두운 모드)** — 전체 통과

| 모드 | 전경 | 배경 | 비율 | 기준 | 결과 |
| --- | --- | --- | --- | --- | --- |
| 어두운 | `text` #E7EEEE | `bg` #101617 | 15.54:1 | 4.5 | 통과 |
| 어두운 | `text` #E7EEEE | `surface` #172021 | 14.12:1 | 4.5 | 통과 |
| 어두운 | `text-muted` #A3B3B4 | `bg` #101617 | 8.40:1 | 4.5 | 통과 |
| 어두운 | `text-muted` #A3B3B4 | `surface` #172021 | 7.63:1 | 4.5 | 통과 |
| 어두운 | `accent` #4FC3BD | `bg` #101617 | 8.59:1 | 4.5 | 통과 |
| 어두운 | `accent` #4FC3BD | `surface` #172021 | 7.80:1 | 4.5 | 통과 |
| 어두운 | `info` #8DB8F0 | `bg` #101617 | 8.91:1 | 4.5 | 통과 |
| 어두운 | `info` #8DB8F0 | `surface` #172021 | 8.10:1 | 4.5 | 통과 |
| 어두운 | `warning` #F2B24C | `bg` #101617 | 9.78:1 | 4.5 | 통과 |
| 어두운 | `warning` #F2B24C | `surface` #172021 | 8.89:1 | 4.5 | 통과 |
| 어두운 | `danger` #FF8A80 | `bg` #101617 | 8.00:1 | 4.5 | 통과 |
| 어두운 | `danger` #FF8A80 | `surface` #172021 | 7.27:1 | 4.5 | 통과 |
| 어두운 | `success` #5FD18A | `bg` #101617 | 9.54:1 | 4.5 | 통과 |
| 어두운 | `success` #5FD18A | `surface` #172021 | 8.67:1 | 4.5 | 통과 |
| 어두운 | `border` #6B7F80 | `bg` #101617 | 4.32:1 | 3.0 | 통과 |
| 어두운 | `border` #6B7F80 | `surface` #172021 | 3.93:1 | 3.0 | 통과 |
| 어두운 | `focus` #7DD8D3 | `bg` #101617 | 10.99:1 | 3.0 | 통과 |
| 어두운 | `focus` #7DD8D3 | `surface` #172021 | 9.98:1 | 3.0 | 통과 |
| 어두운 | `on-accent` #07201F | `accent` #4FC3BD | 7.99:1 | 4.5 | 통과 |

### B. 장부 (남색)

- **의도**: **정확하다**, **통제할 수 있다**. 장부·인쇄물을 떠올리게 하는 남색을 강조색으로 쓴다. `info`는 강조색과 같은 계열이다.
- **기본 색**: 남색 `#24478F`(밝은) / `#8FAEF2`(어두운), 청회색 `#F5F6F8` `#151B26` `#7A8494`, 어두운 바탕 `#0F131A` `#171C25`, 호박색 `#8A5100` / `#F0B44D`, 적색 `#B3261E` / `#FF8F85`, 녹색 `#1E7A3C` / `#62CF8C`
- **피할 기본값과 차이**: 보라 계열과 그라데이션을 쓰지 않고 단색 남색만 쓴다. 파랑 계열은 SaaS 기본값과 겹치기 쉬워서 채도를 낮춘 남색으로 잡았다. 어두운 바탕은 거의 검은색이 아닌 `#0F131A`이고 강조색은 형광이 아닌 옅은 파랑이다.
- **장단점**: 신뢰감을 주는 익숙한 색이다. 단점: 파랑 계열 업무 도구와 구별하기 어렵다. `info`와 `accent`가 같은 색이라 일반 정보와 링크가 색으로는 구분되지 않는다.

**의미 색 매핑**

| 토큰 | 밝은 모드 | 어두운 모드 | 용도 |
| --- | --- | --- | --- |
| `bg` | #F5F6F8 | #0F131A | 화면 바탕 |
| `surface` | #FFFFFF | #171C25 | 카드·메시지·승인 카드 바탕 |
| `text` | #151B26 | #E8ECF2 | 본문 |
| `text-muted` | #4D5666 | #A5AFBE | 보조 설명 |
| `border` | #7A8494 | #6E7A8C | 입력창·카드 테두리(UI 3:1) |
| `divider` | #DCE0E6 | #2A3140 | 장식 구분선(대비 기준 없음, 정보 전달에 쓰지 않음) |
| `accent` | #24478F | #8FAEF2 | 주 버튼·링크 |
| `on-accent` | #FFFFFF | #0C1530 | 주 버튼 위 글자 |
| `focus` | #0B57D0 | #B5CBFF | 포커스 링 |
| `info` | #24478F | #8FAEF2 | 일반 정보 |
| `warning` | #8A5100 | #F0B44D | 외부 출처 경고 |
| `danger` | #B3261E | #FF8F85 | 되돌릴 수 없음 |
| `success` | #1E7A3C | #62CF8C | 성공 |
| `error` | #B3261E | #FF8F85 | 오류. `danger`와 같은 값이며 아이콘(원 안의 X)과 문구로 구분한다 |

**대비표 (밝은 모드)** — 전체 통과

| 모드 | 전경 | 배경 | 비율 | 기준 | 결과 |
| --- | --- | --- | --- | --- | --- |
| 밝은 | `text` #151B26 | `bg` #F5F6F8 | 15.96:1 | 4.5 | 통과 |
| 밝은 | `text` #151B26 | `surface` #FFFFFF | 17.26:1 | 4.5 | 통과 |
| 밝은 | `text-muted` #4D5666 | `bg` #F5F6F8 | 6.84:1 | 4.5 | 통과 |
| 밝은 | `text-muted` #4D5666 | `surface` #FFFFFF | 7.39:1 | 4.5 | 통과 |
| 밝은 | `accent` #24478F | `bg` #F5F6F8 | 8.18:1 | 4.5 | 통과 |
| 밝은 | `accent` #24478F | `surface` #FFFFFF | 8.84:1 | 4.5 | 통과 |
| 밝은 | `info` #24478F | `bg` #F5F6F8 | 8.18:1 | 4.5 | 통과 |
| 밝은 | `info` #24478F | `surface` #FFFFFF | 8.84:1 | 4.5 | 통과 |
| 밝은 | `warning` #8A5100 | `bg` #F5F6F8 | 5.96:1 | 4.5 | 통과 |
| 밝은 | `warning` #8A5100 | `surface` #FFFFFF | 6.44:1 | 4.5 | 통과 |
| 밝은 | `danger` #B3261E | `bg` #F5F6F8 | 6.04:1 | 4.5 | 통과 |
| 밝은 | `danger` #B3261E | `surface` #FFFFFF | 6.53:1 | 4.5 | 통과 |
| 밝은 | `success` #1E7A3C | `bg` #F5F6F8 | 4.97:1 | 4.5 | 통과 |
| 밝은 | `success` #1E7A3C | `surface` #FFFFFF | 5.37:1 | 4.5 | 통과 |
| 밝은 | `border` #7A8494 | `bg` #F5F6F8 | 3.49:1 | 3.0 | 통과 |
| 밝은 | `border` #7A8494 | `surface` #FFFFFF | 3.77:1 | 3.0 | 통과 |
| 밝은 | `focus` #0B57D0 | `bg` #F5F6F8 | 5.90:1 | 3.0 | 통과 |
| 밝은 | `focus` #0B57D0 | `surface` #FFFFFF | 6.38:1 | 3.0 | 통과 |
| 밝은 | `on-accent` #FFFFFF | `accent` #24478F | 8.84:1 | 4.5 | 통과 |

**대비표 (어두운 모드)** — 전체 통과

| 모드 | 전경 | 배경 | 비율 | 기준 | 결과 |
| --- | --- | --- | --- | --- | --- |
| 어두운 | `text` #E8ECF2 | `bg` #0F131A | 15.69:1 | 4.5 | 통과 |
| 어두운 | `text` #E8ECF2 | `surface` #171C25 | 14.40:1 | 4.5 | 통과 |
| 어두운 | `text-muted` #A5AFBE | `bg` #0F131A | 8.39:1 | 4.5 | 통과 |
| 어두운 | `text-muted` #A5AFBE | `surface` #171C25 | 7.70:1 | 4.5 | 통과 |
| 어두운 | `accent` #8FAEF2 | `bg` #0F131A | 8.42:1 | 4.5 | 통과 |
| 어두운 | `accent` #8FAEF2 | `surface` #171C25 | 7.73:1 | 4.5 | 통과 |
| 어두운 | `info` #8FAEF2 | `bg` #0F131A | 8.42:1 | 4.5 | 통과 |
| 어두운 | `info` #8FAEF2 | `surface` #171C25 | 7.73:1 | 4.5 | 통과 |
| 어두운 | `warning` #F0B44D | `bg` #0F131A | 10.05:1 | 4.5 | 통과 |
| 어두운 | `warning` #F0B44D | `surface` #171C25 | 9.22:1 | 4.5 | 통과 |
| 어두운 | `danger` #FF8F85 | `bg` #0F131A | 8.43:1 | 4.5 | 통과 |
| 어두운 | `danger` #FF8F85 | `surface` #171C25 | 7.74:1 | 4.5 | 통과 |
| 어두운 | `success` #62CF8C | `bg` #0F131A | 9.59:1 | 4.5 | 통과 |
| 어두운 | `success` #62CF8C | `surface` #171C25 | 8.80:1 | 4.5 | 통과 |
| 어두운 | `border` #6E7A8C | `bg` #0F131A | 4.27:1 | 3.0 | 통과 |
| 어두운 | `border` #6E7A8C | `surface` #171C25 | 3.92:1 | 3.0 | 통과 |
| 어두운 | `focus` #B5CBFF | `bg` #0F131A | 11.47:1 | 3.0 | 통과 |
| 어두운 | `focus` #B5CBFF | `surface` #171C25 | 10.53:1 | 3.0 | 통과 |
| 어두운 | `on-accent` #0C1530 | `accent` #8FAEF2 | 8.15:1 | 4.5 | 통과 |

### C. 흑연 (무채색)

- **의도**: **미리 보인다**, **차분하다**. 강조색을 흑연색(무채색)으로 두어, 화면에서 색을 가진 요소를 의미 색(`warning`, `danger`, `success`, `info`)으로만 제한한다. 승인 카드에서 외부 출처 경고와 "되돌릴 수 없음"이 가장 눈에 띄는 유채색이 된다.
- **기본 색**: 흑연 `#2E3640`(밝은) / `#D4D9DF`(어두운), 중립 `#F4F5F6` `#17191C` `#7E848C`, 어두운 바탕 `#16181B` `#1E2125`, 포커스 파랑 `#0B63CE` / `#6AA8FF`, 호박색 `#8A5100` / `#F0B44D`, 적색 `#B3261E` / `#FF8F85`, 녹색 `#1E7A3C` / `#62CF8C`
- **피할 기본값과 차이**: 강조색이 보라·파랑이 아니고, 그라데이션과 형광색이 없다. 어두운 바탕은 `#16181B`(회색)이고 강조색은 밝은 회색이라 "거의 검은 배경 + 형광 강조색"과 다르다. 바탕은 크림색이 아닌 중립 회색이다.
- **장단점**: 의미 색이 강조색과 겹치지 않는다. 단점: 브랜드 개성이 색으로는 약하다. 정체성은 로고와 글꼴이 맡아야 한다. 주 버튼(흑연)이 보조 버튼(흰 바탕+테두리)과 색 대비는 크지만 색상 차이는 없어서 문구로 구분해야 한다.

**의미 색 매핑**

| 토큰 | 밝은 모드 | 어두운 모드 | 용도 |
| --- | --- | --- | --- |
| `bg` | #F4F5F6 | #16181B | 화면 바탕 |
| `surface` | #FFFFFF | #1E2125 | 카드·메시지·승인 카드 바탕 |
| `text` | #17191C | #ECEEF0 | 본문 |
| `text-muted` | #51565D | #A9AEB5 | 보조 설명 |
| `border` | #7E848C | #747A83 | 입력창·카드 테두리(UI 3:1) |
| `divider` | #DEE0E3 | #2E3237 | 장식 구분선(대비 기준 없음, 정보 전달에 쓰지 않음) |
| `accent` | #2E3640 | #D4D9DF | 주 버튼·링크 |
| `on-accent` | #FFFFFF | #14171A | 주 버튼 위 글자 |
| `focus` | #0B63CE | #6AA8FF | 포커스 링 |
| `info` | #1F5F99 | #8DB8F0 | 일반 정보 |
| `warning` | #8A5100 | #F0B44D | 외부 출처 경고 |
| `danger` | #B3261E | #FF8F85 | 되돌릴 수 없음 |
| `success` | #1E7A3C | #62CF8C | 성공 |
| `error` | #B3261E | #FF8F85 | 오류. `danger`와 같은 값이며 아이콘(원 안의 X)과 문구로 구분한다 |

**대비표 (밝은 모드)** — 전체 통과

| 모드 | 전경 | 배경 | 비율 | 기준 | 결과 |
| --- | --- | --- | --- | --- | --- |
| 밝은 | `text` #17191C | `bg` #F4F5F6 | 16.13:1 | 4.5 | 통과 |
| 밝은 | `text` #17191C | `surface` #FFFFFF | 17.61:1 | 4.5 | 통과 |
| 밝은 | `text-muted` #51565D | `bg` #F4F5F6 | 6.77:1 | 4.5 | 통과 |
| 밝은 | `text-muted` #51565D | `surface` #FFFFFF | 7.39:1 | 4.5 | 통과 |
| 밝은 | `accent` #2E3640 | `bg` #F4F5F6 | 11.19:1 | 4.5 | 통과 |
| 밝은 | `accent` #2E3640 | `surface` #FFFFFF | 12.22:1 | 4.5 | 통과 |
| 밝은 | `info` #1F5F99 | `bg` #F4F5F6 | 6.09:1 | 4.5 | 통과 |
| 밝은 | `info` #1F5F99 | `surface` #FFFFFF | 6.65:1 | 4.5 | 통과 |
| 밝은 | `warning` #8A5100 | `bg` #F4F5F6 | 5.90:1 | 4.5 | 통과 |
| 밝은 | `warning` #8A5100 | `surface` #FFFFFF | 6.44:1 | 4.5 | 통과 |
| 밝은 | `danger` #B3261E | `bg` #F4F5F6 | 5.98:1 | 4.5 | 통과 |
| 밝은 | `danger` #B3261E | `surface` #FFFFFF | 6.53:1 | 4.5 | 통과 |
| 밝은 | `success` #1E7A3C | `bg` #F4F5F6 | 4.92:1 | 4.5 | 통과 |
| 밝은 | `success` #1E7A3C | `surface` #FFFFFF | 5.37:1 | 4.5 | 통과 |
| 밝은 | `border` #7E848C | `bg` #F4F5F6 | 3.45:1 | 3.0 | 통과 |
| 밝은 | `border` #7E848C | `surface` #FFFFFF | 3.77:1 | 3.0 | 통과 |
| 밝은 | `focus` #0B63CE | `bg` #F4F5F6 | 5.21:1 | 3.0 | 통과 |
| 밝은 | `focus` #0B63CE | `surface` #FFFFFF | 5.69:1 | 3.0 | 통과 |
| 밝은 | `on-accent` #FFFFFF | `accent` #2E3640 | 12.22:1 | 4.5 | 통과 |

**대비표 (어두운 모드)** — 전체 통과

| 모드 | 전경 | 배경 | 비율 | 기준 | 결과 |
| --- | --- | --- | --- | --- | --- |
| 어두운 | `text` #ECEEF0 | `bg` #16181B | 15.29:1 | 4.5 | 통과 |
| 어두운 | `text` #ECEEF0 | `surface` #1E2125 | 13.89:1 | 4.5 | 통과 |
| 어두운 | `text-muted` #A9AEB5 | `bg` #16181B | 7.96:1 | 4.5 | 통과 |
| 어두운 | `text-muted` #A9AEB5 | `surface` #1E2125 | 7.24:1 | 4.5 | 통과 |
| 어두운 | `accent` #D4D9DF | `bg` #16181B | 12.52:1 | 4.5 | 통과 |
| 어두운 | `accent` #D4D9DF | `surface` #1E2125 | 11.38:1 | 4.5 | 통과 |
| 어두운 | `info` #8DB8F0 | `bg` #16181B | 8.67:1 | 4.5 | 통과 |
| 어두운 | `info` #8DB8F0 | `surface` #1E2125 | 7.88:1 | 4.5 | 통과 |
| 어두운 | `warning` #F0B44D | `bg` #16181B | 9.60:1 | 4.5 | 통과 |
| 어두운 | `warning` #F0B44D | `surface` #1E2125 | 8.72:1 | 4.5 | 통과 |
| 어두운 | `danger` #FF8F85 | `bg` #16181B | 8.06:1 | 4.5 | 통과 |
| 어두운 | `danger` #FF8F85 | `surface` #1E2125 | 7.32:1 | 4.5 | 통과 |
| 어두운 | `success` #62CF8C | `bg` #16181B | 9.16:1 | 4.5 | 통과 |
| 어두운 | `success` #62CF8C | `surface` #1E2125 | 8.32:1 | 4.5 | 통과 |
| 어두운 | `border` #747A83 | `bg` #16181B | 4.11:1 | 3.0 | 통과 |
| 어두운 | `border` #747A83 | `surface` #1E2125 | 3.73:1 | 3.0 | 통과 |
| 어두운 | `focus` #6AA8FF | `bg` #16181B | 7.33:1 | 3.0 | 통과 |
| 어두운 | `focus` #6AA8FF | `surface` #1E2125 | 6.66:1 | 3.0 | 통과 |
| 어두운 | `on-accent` #14171A | `accent` #D4D9DF | 12.67:1 | 4.5 | 통과 |

### 팔레트 대비 요약

세 후보 모두 밝은·어두운 모드의 `bg`·`surface` 대비 기준을 전부 통과했다(위 표 참고). 기준 대비 여유가 가장 작은 항목은 `border`(기준 3)와 밝은 모드의 `success`(기준 4.5)다. 각 표에서 확인할 수 있다.

## 3. 글꼴 조합 후보

확인 방법: 배포처 저장소(GitHub)의 라이선스 파일과 `package.json`, 글꼴 파일 목록(GitHub contents API)을 2026-11-11에 `curl`로 직접 받아 확인했다. 글꼴 파일은 받지 않았다.

| 항목 | 후보 1. Pretendard | 후보 2. IBM Plex Sans KR |
| --- | --- | --- |
| 한글·영문·숫자 | 한글·영문·숫자 한 글꼴(라이선스 파일에 Source Han Sans, Inter 등 OFL 글꼴의 파생 표기가 있음) | 한글과 영문·숫자를 한 글꼴 패키지로 제공(영문 글리프의 설계 출처는 확인하지 못함) |
| 굵기 | 가변 글꼴 45~920(CSS `font-weight` 값), 정적 파일은 Thin~Black 9단계 | 정적 파일 Thin, ExtraLight, Light, Regular(400), Text, Medium(500), SemiBold(600), Bold(700) |
| 본문·강조·제목에 필요한 400·500·600·700 | 가변 글꼴 파일 하나로 모두 제공. faux bold 불필요 | 4개 굵기 파일이 각각 있어 모두 제공. faux bold 불필요 |
| 라이선스 | SIL Open Font License 1.1 | SIL Open Font License 1.1 |
| 버전 | 1.3.9 (`package.json`, main 브랜치) | `@ibm/plex-sans-kr` 1.1.0 (`package.json`, master 브랜치) |
| 저작권 표기 | Copyright (c) 2021 Kil Hyung-jin, Reserved Font Name 'Pretendard' | Copyright 2017 IBM Corp., Reserved Font Name "Plex" |
| 라이선스 출처 | https://raw.githubusercontent.com/orioncactus/pretendard/main/LICENSE | https://raw.githubusercontent.com/IBM/plex/master/packages/plex-sans-kr/LICENSE.txt |
| 버전 출처 | https://raw.githubusercontent.com/orioncactus/pretendard/main/package.json | https://raw.githubusercontent.com/IBM/plex/master/packages/plex-sans-kr/package.json |
| woff2 전체 파일 | `PretendardVariable.woff2` 약 2.06MB(2,057,688 B) | 굵기당 약 0.34~0.44MB (Regular 438,540 B, Medium 439,816 B, SemiBold 434,484 B, Bold 370,492 B) |
| 서브셋(`unicode-range` 분할) | 가변 동적 서브셋 92개 파일, 파일당 8~44KB, 전체 합 약 2.96MB. 페이지가 쓰는 범위만 내려받는다 | 굵기당 분할 94개 파일, 파일당 2.5~17.7KB, 굵기당 합 약 0.85~0.93MB. 4개 굵기를 쓰면 합 약 3.6MB이고, 페이지가 쓰는 범위만 내려받는다 |
| 서브셋 파일 출처 | https://raw.githubusercontent.com/orioncactus/pretendard/main/packages/pretendard/dist/web/variable/woff2-dynamic-subset/ | https://raw.githubusercontent.com/IBM/plex/master/packages/plex-sans-kr/fonts/split/woff2/hinted/ |

OFL 1.1 조항(두 파일에서 확인): 임베딩·재배포·수정이 허용되고, 글꼴 단독 판매는 금지되며, 수정한 버전은 Reserved Font Name을 쓰지 못한다. **배포처가 만든 서브셋 파일을 수정 없이 그대로 쓴다.** 직접 서브셋을 만들면 수정 버전이 될 수 있으므로 이름 규정을 따로 확인해야 한다.

**한글 글리프 품질과 숫자 모양**: 본문 크기(14~16px)에서 받침 가독성, 영문·숫자와 섞였을 때의 기준선과 굵기, 숫자 모양은 **확인하지 못했다.** 이 환경에는 두 글꼴이 설치되어 있지 않다. `preview/palettes.html`은 글꼴이 설치되지 않으면 시스템 대체 글꼴로 보인다고 페이지에 표시한다. 두 글꼴을 설치한 PC에서 개발자가 눈으로 확인해야 한다. 시각·날짜 정렬용으로 `font-variant-numeric: tabular-nums`를 쓰지만, 두 글꼴의 해당 OpenType 기능(`tnum`) 지원 여부도 글꼴 파일을 열어 보지 못해 **미확인**이다.

**선택 이유 기록**: Pretendard를 후보에 넣은 이유는 가변 글꼴 하나로 굵기를 모두 얻고 한국 웹에서 검증된 서브셋 배포 방식이 있어서다. IBM Plex Sans KR을 넣은 이유는 한글과 영문·숫자를 한 패키지로 제공하고, 정적 굵기 4개만 골라 쓸 수 있어서다.

**대체 글꼴 순서**

- 후보 1: `"Pretendard Variable", Pretendard, -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans KR", sans-serif`
- 후보 2: `"IBM Plex Sans KR", -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans KR", sans-serif`
- 주소·코드용: `ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace` (웹 글꼴 없음)

**한글 조판 기본값** (`tokens.css`에 정의)

- `word-break: keep-all`과 `overflow-wrap: anywhere`를 함께 쓴다. 메일 주소·URL이 화면을 넘치지 않게 한다.
- 본문 줄 간격 1.7, 제목 1.35.
- 본문 `max-width` 44em (한글 한 글자를 대략 1em으로 보고 한 줄 40~50자).
- 자간은 글꼴 기본값(0). 제목도 0으로 시작하고, 바꾸면 토큰으로 둔다.

## 4. 로고

모든 SVG는 `viewBox`만 두고 크기를 고정하지 않았다. 외부 글꼴·이미지 참조가 없고, 단색 채우기이며, 기호는 `<g id="mark">`, 글자는 `<g id="wordmark">`로 묶었다. 밝은 배경용(`#17191C`)과 어두운 배경용(`-on-dark`, `#ECEEF0`) 파일을 따로 둔다. `<img>`로 불러온 SVG는 `currentColor`를 상속하지 못해서 색을 파일에 고정했다.

| 상태 | 안 | 파일 | 형태와 의미 | 궤도·고리와의 차이 |
| --- | --- | --- | --- | --- |
| **채택** | 로고마크 A 게이트 | `logo/mark-gate.svg` | 오른쪽이 열린 네모 틀과 열린 자리에 놓인 점. 요청이 승인 지점에서 멈춰 있다는 뜻 | 원·고리가 없고 각진 형태 |
| **채택** | 워드마크 `Orbit` | `logo/word-title.svg` | IBM Plex Sans KR SemiBold 글리프의 윤곽선(path), 자간 -0.5 | |
| **채택** | 조합형 | `logo/combo-gate.svg` | 게이트 + `Orbit`(윤곽선) | |
| 기각 | 로고마크 B 조각 | `logo/mark-slice.svg` | 3/4 원과 떨어져 나온 1/4 조각 | 원형 차트와 비슷해 보일 수 있음 |
| 기각 | 로고마크 C 어긋난 O | `logo/mark-shift.svg` | 두 반고리가 서로 반대로 밀린 O | 고리 계열이라 다른 서비스 로고와 겹칠 위험이 가장 큼 |
| 기각 | 워드마크 `orbit` | `logo/word-lower.svg` | 소문자 굵기 700, `<text>` 초안 | |

기각한 SVG 파일은 이력 보존을 위해 `logo/`에 그대로 두었다. 지우거나 옮기지 않았다.

**워드마크 윤곽선 변환 (완료)**
- 글꼴 파일: 공식 배포처 IBM/plex 저장소의 `packages/plex-sans-kr/fonts/complete/woff2/hinted/IBMPlexSansKR-SemiBold.woff2` (434,484 B, 글꼴 이름 `IBM Plex Sans KR SmBld`, Version 1.003). 출처 URL: https://raw.githubusercontent.com/IBM/plex/master/packages/plex-sans-kr/fonts/complete/woff2/hinted/IBMPlexSansKR-SemiBold.woff2 (릴리스 태그가 아니라 master 브랜치 파일이다). 라이선스는 SIL OFL 1.1(3절 참고).
- 도구: Python `fontTools`(SVGPathPen)로 `O r b i t` 다섯 글리프의 윤곽을 font-size 34 기준으로 변환했다. 글꼴 파일과 변환 도구는 저장소에 넣지 않았다.
- 한계: 글리프를 하나씩 배치했고 **GPOS 커닝은 적용하지 않았다**(글꼴에 GPOS 테이블이 있음). 글자 사이 간격은 기본 advance와 자간 -0.5만 반영한다. 글자 간격이 어색하면 손으로 조정해야 한다.
- OFL의 Reserved Font Name 조항: 글꼴 파일을 수정해 배포한 것이 아니라 글리프 윤곽을 로고 그림으로 쓴 것이다. 이 해석이 맞는지는 확정하지 못했다(미확인).

- `rsvg-convert`가 없어 PNG는 만들지 않았다.
- 기존 상표나 로고와의 유사성은 검색하지 못했다. 출시 전 상표 확인이 필요하다.

## 5. 선택 결과

개발자가 2026-11-11에 추천 조합을 선택했다. **팔레트 C 흑연, 글꼴 IBM Plex Sans KR, 로고마크 A 게이트, 워드마크 `Orbit`(대문자 시작), 조합형 게이트 + `Orbit`.** `tokens.json`, `tokens.css`는 이 선택으로 확정했다.

선택 이유(추천 당시 근거):
- 팔레트: 강조색이 무채색이라 화면의 유채색이 의미 색뿐이다. 승인 카드의 외부 출처 경고와 "되돌릴 수 없음"이 먼저 눈에 들어오므로 "미리 보인다", "통제할 수 있다"에 연결된다. 모든 대비 기준을 통과했다. 약점은 색으로 만드는 브랜드 개성이 약하다는 점이고, 로고와 글꼴로 보완해야 한다.
- 글꼴: 한글과 영문·숫자를 한 패키지로 제공하고 Pretendard·Inter 조합이라는 기본값에서 벗어난다. 한글 글리프 품질은 확인하지 못했다. 굵기 4개를 쓰면 서브셋 파일 수와 용량이 Pretendard 가변 글꼴보다 불리할 수 있으며 측정하지 않았다.
- 로고: 고리·궤도를 쓰지 않아 흔한 방향을 피한다. 열린 틀과 멈춘 점이 "승인 전에 멈춘다"를 나타낸다.

## 6. 사용 규칙

- UI 코드의 색과 글꼴 값은 `tokens.css`의 토큰으로만 쓴다. 토큰에 없는 값이 필요하면 토큰을 먼저 추가한다.
- `warning`, `danger`, `error`는 색만으로 구분하지 않는다. 아이콘과 문구를 함께 쓴다.
- 값을 바꾸면 `tokens.json`과 `tokens.css`를 같은 커밋에서 바꾸고 대비표를 다시 계산한다.

## 7. 남은 일

- 출시 전 상표·유사 로고 확인.
- IBM Plex Sans KR을 설치한 환경에서 한글 본문(14~16px)과 숫자 모양, `tabular-nums`(`tnum`) 동작 확인. 문제가 있으면 글꼴 결정을 다시 연다.
- 워드마크 커닝 확인과 필요하면 조정.
- 포커스 링과 `accent` 버튼이 맞닿는 경우의 대비 확인.
- 프론트엔드에 적용할 때 서브셋 파일의 자체 호스팅 위치와 `@font-face` 작성(프론트엔드 스택 결정과 함께).
- 윤곽선 변환이 OFL Reserved Font Name 조항에 문제가 없는지 확인.

## 결정 기록

### 2026-11-11 팔레트
- 상태: 채택
- 채택안: C 흑연 — 강조색이 무채색이라 의미 색이 가장 먼저 보인다("미리 보인다"). 외부 쓰기 승인 화면에서 경고를 놓치지 않는 것이 "통제할 수 있다"로 이어진다. 대비 기준 전 항목 통과.
- 기각안: A 하버(청록) — 강조색과 `success` 녹색이 색각 이상 사용자에게 비슷하게 보일 수 있다. B 장부(남색) — `info`와 `accent`가 같은 색이고 파랑 계열 SaaS 기본값과 겹친다. 두 안 모두 대비 기준은 통과했다.
- 선택한 사람: 개발자 (2026-11-11)
- 남은 일: 포커스 링과 `accent` 버튼 접촉 대비 확인

### 2026-11-11 글꼴
- 상태: 채택
- 채택안: IBM Plex Sans KR — 한글과 영문·숫자를 한 패키지로 제공하고 정적 굵기(400·500·600·700)가 있다. 기본값(Pretendard·Inter)을 피한다. 라이선스 SIL OFL 1.1, 버전 1.1.0.
- 기각안: Pretendard 1.3.9 — 개발자가 추천안을 선택했다. 별도 기각 사유는 받지 못했다. 한글 글리프 품질 비교는 하지 못했다.
- 선택한 사람: 개발자 (2026-11-11)
- 남은 일: 설치 환경에서 한글·숫자 확인(`tnum` 포함), 서브셋 자체 호스팅 방식 결정

### 2026-11-11 로고
- 상태: 채택
- 채택안: 로고마크 A 게이트, 워드마크 `Orbit`(대문자 시작), 조합형 게이트 + `Orbit` — 고리·궤도를 피하고, 열린 틀과 멈춘 점이 "승인 전에 멈춘다"를 나타낸다("통제할 수 있다"). 워드마크는 Plex Sans KR SemiBold 윤곽선으로 변환했다.
- 기각안: 로고마크 B 조각(원형 차트와 혼동 가능), 로고마크 C 어긋난 O(고리 계열이라 기존 로고와 겹칠 위험), 워드마크 `orbit` 소문자(개발자가 대문자 시작을 선택). SVG는 `logo/`에 남겼다.
- 선택한 사람: 개발자 (2026-11-11)
- 남은 일: 상표·유사 로고 확인(출시 전), 워드마크 커닝 확인, PNG 생성(`rsvg-convert` 필요)

### 2026-11-11 제품 이름
- 상태: 채택
- 채택안: 이름 **Orbit** 유지, 로고 게이트 유지 — "사용자를 중심으로 돌되, 밖으로 나갈 때는 멈춘다". 이름(사용자 중심)과 로고(승인 전 멈춤)는 "주도권은 사용자에게 있다"는 같은 성격을 다른 면에서 보여 준다.
- 검토한 대안: 로고를 이름에 맞춰 다시 그리기(승인 성격이 약해짐), 승인·통제 쪽 이름으로 변경(Relay, Steward, Harbor 등, 상표 충돌 가능성이 큼).
- 선택한 사람: 개발자
- 남은 일: 출시 전 상표 확인. 화면에 보이는 제품 이름은 프론트엔드 한 곳에서만 정의해 바꿀 수 있게 둔다.
