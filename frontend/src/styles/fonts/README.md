# 자체 호스팅 글꼴: IBM Plex Sans KR

- 글꼴: IBM Plex Sans KR, 패키지 `@ibm/plex-sans-kr` **1.1.0**
- 라이선스: SIL Open Font License 1.1 (같은 폴더의 `LICENSE.txt`, 저작권 표기 "Copyright © 2017 IBM Corp. with Reserved Font Name "Plex"")
- 배포처: IBM/plex 저장소, 릴리스 태그 `@ibm/plex-sans-kr@1.1.0`, 커밋 `1da12f02587b630c07e92692d21492d722f53614`
- 파일 경로(배포처): `packages/plex-sans-kr/fonts/split/woff2/hinted/IBMPlexSansKR-{Regular,Medium,SemiBold}-NN.woff2`
- 굵기: Regular 400, Medium 500, SemiBold 600. 화면에서 쓰는 굵기만 담았다(Bold 700은 쓰지 않아 뺐다).
- 수정하지 않았다. 배포처가 만든 분할 파일을 그대로 복사했다. `../fonts.css`의 `unicode-range`는 같은 버전의 npm 패키지 `css/ibm-plex-sans-kr-all.css`에서 가져왔고, `local()` 대체 경로와 woff 경로는 뺐다.
- 검증(2026-11-11): woff2 282개 전부를 위 커밋의 GitHub raw 파일(`.../packages/plex-sans-kr/fonts/split/woff2/hinted/<파일명>`)과 SHA-256으로 대조했다. 일치 282, 불일치 0, 내려받기 실패 0. `LICENSE.txt`도 같은 커밋의 raw 파일과 SHA-256이 같다.
- 재생성: `npm pack @ibm/plex-sans-kr@1.1.0`으로 받은 패키지에서 위 세 굵기의 분할 파일과 `unicode-range`를 다시 뽑는다. 외부 CDN은 쓰지 않는다.
