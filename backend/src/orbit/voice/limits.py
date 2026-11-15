"""음성 입력 상수 (ADR-0025 §2·§3). `실험 중` 기본값이며 한곳에서만 정한다.

사용량 상한 두 가지는 `Settings`(`ORBIT_VOICE_MAX_TRANSCRIBES_PER_HOUR`,
`ORBIT_VOICE_MAX_SECONDS_PER_DAY`)로 바꿀 수 있고, 여기 값이 기본값이다.
"""

from datetime import timedelta

MAX_AUDIO_BYTES = 2 * 1024 * 1024  # WAV 업로드 상한(ADR-0025의 2MB)
# webm·ogg·mp4는 길이를 서버가 알 수 없다. 프론트가 `audioBitsPerSecond: 32000`으로 녹음하므로
# 60초는 약 240KB이고, 여유를 둔 이 크기가 길이 상한을 대신한다.
MAX_COMPRESSED_AUDIO_BYTES = 512 * 1024
MAX_AUDIO_SECONDS = 60.0  # 녹음 길이 상한
# multipart 경계·헤더 몫. 본문 전체가 상한 + 이 값을 넘으면 읽기를 멈추고 거절한다.
MULTIPART_OVERHEAD_BYTES = 16 * 1024
# 길이를 알 수 없는 형식(webm·ogg·mp4)의 사용 시간 추정: 비트레이트를 32kbps(초당 4000바이트)로
# 가정해 `바이트 / 4000`초로 본다(1~60초로 자름). 32kbps보다 높은 오디오는 실제보다 길게, 낮은
# 오디오는 짧게 잡힌다. 실제 길이를 제공자가 알려 주면 기록을 그 값으로 보정한다(`usage.correct`).
ASSUMED_BYTES_PER_SECOND = 4000

DEFAULT_MAX_TRANSCRIBES_PER_HOUR = 60
DEFAULT_MAX_SECONDS_PER_DAY = 30 * 60.0

HOUR_WINDOW = timedelta(hours=1)
DAY_WINDOW = timedelta(hours=24)  # 달력 날짜가 아니라 최근 24시간
# 이보다 오래된 사용량 행은 새 기록 때 같은 워크스페이스 안에서 지운다
USAGE_RETENTION = timedelta(days=2)

VOICE_CONFIG: dict[str, int | float] = {
    "max_audio_bytes": MAX_AUDIO_BYTES,
    "max_audio_seconds": MAX_AUDIO_SECONDS,
    "default_max_transcribes_per_hour": DEFAULT_MAX_TRANSCRIBES_PER_HOUR,
    "default_max_seconds_per_day": DEFAULT_MAX_SECONDS_PER_DAY,
}
