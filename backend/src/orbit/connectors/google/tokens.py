"""액세스 토큰 공급자 계약 (ADR-0016 3·4절). 구현은 6B(연결·봉투 암호화)가 맡는다.

어댑터는 토큰 원문을 저장하거나 기록하지 않고, 요청마다 공급자에게서 받아 헤더에만 쓴다.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class AccessTokenProvider(Protocol):
    async def access_token(self) -> str:
        """현재 유효하다고 믿는 액세스 토큰."""
        ...

    async def refresh(self) -> str:
        """리프레시 토큰으로 새 액세스 토큰을 받는다. 철회·실패면 예외를 던진다."""
        ...
