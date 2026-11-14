# 자체 호스팅 글꼴: Pretendard

- 글꼴: Pretendard, 패키지 `pretendard` **1.3.9**
- 라이선스: SIL Open Font License 1.1 (같은 폴더의 `LICENSE.txt`, 원본은 `dist/LICENSE.txt`, 저작권 표기 "Copyright (c) 2021, Kil Hyung-jin, Reserved Font Name Pretendard")
- 배포처: orioncactus/pretendard 저장소의 npm 배포본. `npm pack pretendard@1.3.9`
- 파일 경로(배포처): `dist/web/static/woff2-dynamic-subset/Pretendard-{Regular,Medium,SemiBold}.subset.N.woff2` 276개(굵기마다 92개). 패키지의 `dist/web/static/pretendard-dynamic-subset.css`에서 `unicode-range`를 가져왔다.
- 굵기: Regular 400, Medium 500, SemiBold 600. Bold 700 이상은 쓰지 않아 뺐다.
- 수정하지 않았다. 패키지의 woff2를 그대로 복사했다. `local()`과 woff·ttf 경로는 `../../fonts.css`에서 뺐다.
- 검증(2026-11-13): 받은 tarball의 sha512 무결성 값이 `npm view pretendard@1.3.9 dist.integrity`와 같다(sha512-PaQAADyLY5v4kYFwkpSJHbSSYIkiriY/1xXw75TKoZ9UQQqeU+tvP05yTdZAWibiIYoo8ZKtRv8PM7w0IaywSw==). 복사한 woff2 276개는 풀어 둔 패키지의 같은 파일과 바이트 단위로 모두 같다(cmp)(일치 276, 불일치 0). GitHub 원본 저장소와는 대조하지 않았다.
- 재생성: `npm pack pretendard@1.3.9`로 받은 패키지에서 위 파일을 다시 복사하고 `unicode-range`를 다시 뽑는다. 외부 CDN은 쓰지 않는다.
