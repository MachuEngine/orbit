import struct

import httpx
import pytest

from audio_support import mp4, ogg, wav, webm
from orbit.voice.limits import MAX_AUDIO_BYTES, MAX_COMPRESSED_AUDIO_BYTES
from orbit.voice.upload import AudioUploadParser, UploadError, parse_audio_upload


def _multipart(
    data: bytes, content_type: str, *, name: str = "audio", extra: bool = False
) -> tuple[str, bytes]:
    files = [(name, ("a.bin", data, content_type))]
    if extra:
        files.append(("other", ("b.bin", data, content_type)))
    request = httpx.Request("POST", "http://t/x", files=files)
    return request.headers["content-type"], request.read()


@pytest.mark.parametrize(
    ("data", "claimed", "expected"),
    [
        (webm(), "audio/webm;codecs=opus", "audio/webm"),
        (ogg(), "audio/ogg", "audio/ogg"),
        (wav(1), "audio/x-wav", "audio/wav"),
        (mp4(), "audio/mp4", "audio/mp4"),
        (mp4(), "audio/x-m4a", "audio/mp4"),
    ],
)
def test_accepts_supported_formats(data: bytes, claimed: str, expected: str) -> None:
    header, body = _multipart(data, claimed)
    audio = parse_audio_upload(header, body)
    assert audio.data == data
    assert audio.content_type == expected


def test_binary_payload_survives_roundtrip() -> None:
    data = webm(0) + b"\r\n--x\r\n\r\n\x00\xff\xfe" + bytes(range(256)) * 40 + b"\r\n"
    header, body = _multipart(data, "audio/webm")
    assert parse_audio_upload(header, body).data == data


def test_wav_duration_from_header_and_unknown_for_others() -> None:
    header, body = _multipart(wav(3), "audio/wav")
    assert parse_audio_upload(header, body).duration_seconds == pytest.approx(3.0)
    header, body = _multipart(webm(), "audio/webm")
    assert parse_audio_upload(header, body).duration_seconds is None


def test_claimed_type_must_match_magic_bytes() -> None:
    header, body = _multipart(b"not audio at all", "audio/webm")
    with pytest.raises(UploadError) as err:
        parse_audio_upload(header, body)
    assert err.value.kind == "unsupported_format"
    assert err.value.status_code == 400
    header, body = _multipart(ogg(), "audio/webm")  # 주장과 실제가 다르다
    with pytest.raises(UploadError):
        parse_audio_upload(header, body)


@pytest.mark.parametrize("claimed", ["text/plain", "application/octet-stream", "audio/mpeg"])
def test_rejects_unlisted_types(claimed: str) -> None:
    header, body = _multipart(webm(), claimed)
    with pytest.raises(UploadError) as err:
        parse_audio_upload(header, body)
    assert err.value.kind == "unsupported_format"


def test_compressed_formats_have_a_lower_size_cap_than_wav() -> None:
    ok = _multipart(webm(MAX_COMPRESSED_AUDIO_BYTES), "audio/webm")
    assert len(parse_audio_upload(*ok).data) == MAX_COMPRESSED_AUDIO_BYTES
    for data, ctype in (
        (webm(MAX_COMPRESSED_AUDIO_BYTES + 1), "audio/webm"),
        (ogg(600_000), "audio/ogg"),
    ):
        with pytest.raises(UploadError) as err:
            parse_audio_upload(*_multipart(data, ctype))
        assert (err.value.kind, err.value.status_code) == ("too_large", 413)
    big_wav = wav(60)  # 480KB: 압축 형식 상한을 넘어도 WAV는 2MB까지 허용
    assert len(parse_audio_upload(*_multipart(big_wav, "audio/wav")).data) == len(big_wav)


def test_wav_over_2mb_is_rejected_with_413() -> None:
    with pytest.raises(UploadError) as err:
        parse_audio_upload(*_multipart(wav(60) + b"\x80" * MAX_AUDIO_BYTES, "audio/wav"))
    assert (err.value.kind, err.value.status_code) == ("too_large", 413)


def test_wav_duration_uses_actual_bytes_not_header_claim() -> None:
    # 헤더는 1초라고 선언하지만 본문은 120초 분량이다.
    forged = bytearray(wav(120))
    struct.pack_into("<I", forged, 40, 8000)  # data 청크 선언 크기
    struct.pack_into("<I", forged, 4, 8000 + 36)  # RIFF 크기
    audio = parse_audio_upload(*_multipart(bytes(forged), "audio/wav"))
    assert audio.duration_seconds == pytest.approx(120.0)


def test_wav_without_parsable_header_is_rejected() -> None:
    broken = b"RIFF\x00\x00\x00\x00WAVEjunk" + b"\x00" * 40
    with pytest.raises(UploadError) as err:
        parse_audio_upload(*_multipart(broken, "audio/wav"))
    assert err.value.kind == "unsupported_format"


def test_streaming_feed_in_small_chunks_matches_whole_parse() -> None:
    data = webm(5000)
    header, body = _multipart(data, "audio/webm")
    parser = AudioUploadParser(header)
    for i in range(0, len(body), 7):
        parser.feed(body[i : i + 7])
    assert parser.finish().data == data


def test_rejects_bad_structure() -> None:
    header, body = _multipart(webm(), "audio/webm", name="file")
    for h, b in (
        (header, body),  # 필드 이름이 다르다
        (*_multipart(webm(), "audio/webm", extra=True),),  # 파트가 둘이다
        ("application/json", b"{}"),
        ("multipart/form-data; boundary=zzz", b"garbage"),
        ("multipart/form-data", b""),
    ):
        with pytest.raises(UploadError) as err:
            parse_audio_upload(h, b)
        assert err.value.kind == "bad_request"
