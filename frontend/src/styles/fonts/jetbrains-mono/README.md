# 자체 호스팅 글꼴: JetBrains Mono

- 글꼴: JetBrains Mono, 패키지 `@fontsource/jetbrains-mono` **5.3.0**
- 라이선스: SIL Open Font License 1.1 (같은 폴더의 `LICENSE`, 원본은 패키지 루트 `LICENSE`, 저작권 표기 "Copyright 2020 The JetBrains Mono Project Authors")
- 배포처: Fontsource 패키지(fontsource/font-files, `fonts/google/jetbrains-mono`). `npm pack @fontsource/jetbrains-mono@5.3.0`
- 파일 경로(배포처): `files/jetbrains-mono-{latin,latin-ext}-{400,500}-normal.woff2` 4개. `unicode-range`는 패키지의 `400.css`, `500.css`에서 가져왔다.
- 굵기: Regular 400, Medium 500.
- 수정하지 않았다. 패키지의 woff2를 그대로 복사했다. `local()`과 woff·ttf 경로는 `../../fonts.css`에서 뺐다.
- 검증(2026-11-13): 받은 tarball의 sha512 무결성 값이 `npm view @fontsource/jetbrains-mono@5.3.0 dist.integrity`와 같다(sha512-fqDfB5I9f1p1TV486aUgB9t8zP84P0O1FtQR5Ol9vjwPy+S+EIGlVYm1cvj2W5shcZMTg2nZFdVMoH5wFu8a1A==). 복사한 woff2 4개는 풀어 둔 패키지의 같은 파일과 바이트 단위로 모두 같다(cmp)(일치 4, 불일치 0). GitHub 원본 저장소와는 대조하지 않았다.
- 재생성: `npm pack @fontsource/jetbrains-mono@5.3.0`로 받은 패키지에서 위 파일을 다시 복사하고 `unicode-range`를 다시 뽑는다. 외부 CDN은 쓰지 않는다.
