"""합성 테스트 오디오. 무음이거나 형식 머리말만 있는 바이트다(실제 음성 아님)."""

import io
import wave


def wav(seconds: float, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(rate)
        w.writeframes(b"\x80" * int(seconds * rate))
    return buf.getvalue()


def webm(size: int = 64) -> bytes:
    """머리말 4바이트를 포함해 정확히 `size`바이트(최소 4)다."""
    return (b"\x1a\x45\xdf\xa3" + bytes(range(256)) * (size // 256 + 1))[: max(size, 4)]


def ogg(size: int = 64) -> bytes:
    return b"OggS" + b"\x00" * size


def mp4(size: int = 64) -> bytes:
    return b"\x00\x00\x00\x18ftypM4A " + b"\x00" * size
