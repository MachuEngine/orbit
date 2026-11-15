"""음성 업로드 해석 (ADR-0025 §2·§3). `python-multipart`의 스트리밍 파서로 multipart를 읽는다.

- 오디오는 메모리에서만 다룬다. Starlette `UploadFile`은 1MB를 넘으면 임시 파일을 만들 수 있어
  쓰지 않고, 파서 콜백으로 바이트를 메모리 버퍼에 모은다. 이 모듈은 디스크·로그에 쓰지 않는다.
- 본문 크기 상한은 호출자가 청크를 `feed`하는 동안 강제한다(상한을 넘으면 읽기를 중단).
- 형식은 요청이 주장한 MIME 형식과 파일 앞부분(매직 바이트)이 모두 맞아야 통과한다.
- 길이는 WAV만 알 수 있다. 헤더가 선언한 프레임 수가 아니라 data 청크 뒤의 실제 바이트 수로
  계산한다(헤더를 줄이고 본문을 채운 파일이 상한을 피하지 못하게). 그 밖의 형식은 `None`이고
  호출자가 크기 상한으로 대신한다.
"""

import struct
from dataclasses import dataclass

from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from orbit.voice.limits import MAX_AUDIO_BYTES, MAX_COMPRESSED_AUDIO_BYTES

FIELD_NAME = "audio"
_WAV_FORMATS = (1, 3)  # PCM, IEEE float
_WAV_RATE_RANGE = (8000, 48000)
_MAX_HEADER_BYTES = 4096

# 주장된 MIME 형식 → 표준 형식(OpenAI에 넘기는 값)
_CLAIMED = {
    "audio/webm": "audio/webm",
    "video/webm": "audio/webm",
    "audio/ogg": "audio/ogg",
    "application/ogg": "audio/ogg",
    "audio/wav": "audio/wav",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/vnd.wave": "audio/wav",
    "audio/mp4": "audio/mp4",
    "audio/x-m4a": "audio/mp4",
    "audio/m4a": "audio/mp4",
    "video/mp4": "audio/mp4",
}


class UploadError(Exception):
    """업로드 검증 실패. `kind`와 HTTP 상태만 노출한다."""

    def __init__(self, kind: str, status_code: int = 400) -> None:
        super().__init__(kind)
        self.kind = kind
        self.status_code = status_code


@dataclass(frozen=True)
class Audio:
    data: bytes
    content_type: str  # 표준 형식
    duration_seconds: float | None  # 알 수 없으면 None


def _sniff(data: bytes) -> str | None:
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/webm"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[4:8] == b"ftyp":
        return "audio/mp4"
    return None


def _wav_duration(data: bytes) -> float | None:
    """`fmt ` 청크의 초당 바이트 수와 data 청크 시작 뒤의 실제 바이트 수로 길이를 구한다.

    data 청크가 선언한 크기는 쓰지 않는다. 뒤에 다른 청크가 붙어 있으면 길이를 더 길게 잡는다
    (상한 판정에 불리한 쪽). 해석하지 못하면 None이다.
    """
    pos = 12
    byte_rate = 0
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        (declared,) = struct.unpack_from("<I", data, pos + 4)
        body = pos + 8
        if chunk_id == b"fmt ":
            byte_rate = _validated_byte_rate(data, body)
        elif chunk_id == b"data":
            return (len(data) - body) / byte_rate if byte_rate > 0 else None
        pos = body + declared + (declared & 1)
    return None


def _validated_byte_rate(data: bytes, body: int) -> int:
    """`fmt ` 청크의 값이 서로 맞고 범위 안일 때만 초당 바이트 수를 돌려준다. 아니면 0이다.

    PCM(1)과 IEEE float(3)만, 채널 1~2, 8~48kHz, 8·16·24·32비트를 허용한다. 헤더의
    `byte_rate`를 위조해 길이를 짧게 보이게 하는 파일을 막으려고 `block_align`과 `byte_rate`가
    나머지 값에서 계산한 값과 같은지 확인한다.
    """
    if body + 16 > len(data):
        return 0
    audio_format, channels, rate, byte_rate, block_align, bits = struct.unpack_from(
        "<HHIIHH", data, body
    )
    if (
        audio_format not in _WAV_FORMATS
        or channels not in (1, 2)
        or not _WAV_RATE_RANGE[0] <= rate <= _WAV_RATE_RANGE[1]
        or bits not in (8, 16, 24, 32)
        or block_align != channels * bits // 8
        or byte_rate != rate * block_align
    ):
        return 0
    return int(byte_rate)


class AudioUploadParser:
    """multipart 본문을 청크로 받아 `audio` 파트 하나를 꺼낸다. 오류는 `UploadError`다."""

    def __init__(self, content_type_header: str) -> None:
        media, params = parse_options_header(content_type_header)
        boundary = params.get(b"boundary")
        if media != b"multipart/form-data" or not boundary:
            raise UploadError("bad_request")
        self._parts = 0
        self._headers: dict[bytes, bytes] = {}
        self._field = b""
        self._value = b""
        self._name: bytes | None = None
        self._claimed = ""
        self._buffer = bytearray()
        self._oversize = False
        self._parser = MultipartParser(
            boundary,
            {
                "on_part_begin": self._on_part_begin,
                "on_header_field": self._on_header_field,
                "on_header_value": self._on_header_value,
                "on_header_end": self._on_header_end,
                "on_headers_finished": self._on_headers_finished,
                "on_part_data": self._on_part_data,
            },
            max_header_size=_MAX_HEADER_BYTES,
        )

    def _on_part_begin(self) -> None:
        self._parts += 1
        self._headers = {}
        self._field = self._value = b""

    def _on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._field += data[start:end]

    def _on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def _on_header_end(self) -> None:
        self._headers[self._field.lower()] = self._value
        self._field = self._value = b""

    def _on_headers_finished(self) -> None:
        _, disposition = parse_options_header(self._headers.get(b"content-disposition"))
        self._name = disposition.get(b"name")
        media, _ = parse_options_header(self._headers.get(b"content-type"))
        self._claimed = media.decode("latin-1").lower()

    def _on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._parts != 1:
            return
        self._buffer += data[start:end]
        if len(self._buffer) > MAX_AUDIO_BYTES:
            self._oversize = True  # 더 쌓지 않고 `finish`에서 거절한다
            del self._buffer[MAX_AUDIO_BYTES + 1 :]

    def feed(self, chunk: bytes) -> None:
        try:
            self._parser.write(chunk)
        except (MultipartParseError, ValueError):
            raise UploadError("bad_request") from None

    def finish(self) -> Audio:
        try:
            self._parser.finalize()
        except (MultipartParseError, ValueError):
            raise UploadError("bad_request") from None
        if self._parts != 1 or self._name != FIELD_NAME.encode() or not self._buffer:
            raise UploadError("bad_request")
        payload = bytes(self._buffer)
        expected = _CLAIMED.get(self._claimed)
        if self._oversize and expected != "audio/wav":
            raise UploadError("too_large", 413)
        if expected is None or _sniff(payload) != expected:
            raise UploadError("unsupported_format")
        if self._oversize:
            raise UploadError("too_large", 413)
        if expected == "audio/wav":
            duration = _wav_duration(payload)
            if duration is None:
                raise UploadError("unsupported_format")
            return Audio(data=payload, content_type=expected, duration_seconds=duration)
        if len(payload) > MAX_COMPRESSED_AUDIO_BYTES:
            raise UploadError("too_large", 413)
        return Audio(data=payload, content_type=expected, duration_seconds=None)


def parse_audio_upload(content_type_header: str, body: bytes) -> Audio:
    """본문 전체가 이미 메모리에 있을 때의 편의 함수(테스트용)."""
    parser = AudioUploadParser(content_type_header)
    parser.feed(body)
    return parser.finish()
