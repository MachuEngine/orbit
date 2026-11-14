# 자체 호스팅 글꼴: Galmuri11

- 글꼴: Galmuri11, 패키지 `galmuri` **2.40.3**
- 라이선스: SIL Open Font License 1.1 (같은 폴더의 `LICENSE.txt`, 원본은 `dist/LICENSE.txt`, 저작권 표기 "Copyright (c) 2019-2025 Lee Minseo")
- 배포처: quiple/galmuri 저장소의 npm 배포본. `npm pack galmuri@2.40.3`
- 파일 경로(배포처): `dist/Galmuri11.woff2` 1개(분할 없음). 패키지 CSS에 `unicode-range`가 없어, 파일의 cmap을 읽어 실제로 덮는 구간(기초 라틴, 라틴-1, 일반 문장부호, 화살표, 선·도형, CJK 기호, 한글 자모, 한글 음절 U+AC00-D7A3 전체)만 `../../fonts.css`에 적었다. 한자와 그 밖의 구간은 일부만 덮어 범위에 넣지 않았다.
- 굵기: Regular 400. Bold는 쓰지 않아 뺐다.
- 수정하지 않았다. 패키지의 woff2를 그대로 복사했다. `local()`과 woff·ttf 경로는 `../../fonts.css`에서 뺐다.
- 검증(2026-11-13): 받은 tarball의 sha512 무결성 값이 `npm view galmuri@2.40.3 dist.integrity`와 같다(sha512-eXflXih1AWy2J+jWeT5sGVj4PUCb1bMK7lnwKDKMzEd3lx+F6myuxqp6c0eZlD7EMXOicUYaM+FH7DAoQ7mWbw==). 복사한 woff2 1개는 풀어 둔 패키지의 같은 파일과 바이트 단위로 모두 같다(cmp)(일치 1, 불일치 0). GitHub 원본 저장소와는 대조하지 않았다.
- 재생성: `npm pack galmuri@2.40.3`로 받은 패키지에서 위 파일을 다시 복사하고 `unicode-range`를 다시 뽑는다. 외부 CDN은 쓰지 않는다.
