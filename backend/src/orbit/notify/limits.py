"""알림 상수 (ADR-0021 §3·§4·§5). `실험 중` 기본값이며 한곳에서만 정한다.

값을 바꾸면 `NOTIFY_CONFIG`가 바뀌어 `run.config_fingerprint`도 달라진다(재현성, D9-3).
"""

from datetime import timedelta

LEAD_MINUTES_CHOICES = (5, 10, 15, 30, 60)  # 일정 시작 몇 분 전에 알릴지(사용자 선택)
DEFAULT_LEAD_MINUTES = 10
DEFAULT_EVENT_REMINDERS_ENABLED = True

# 일정 알림 동기화 (ADR-0021 §4)
SYNC_WINDOW = timedelta(hours=2)  # 지금부터 이 시간 안에 시작하는 일정만 본다
TICK_CRON = "*/5 * * * *"  # 대상 선별 주기(5분)

# 알림 제목·목록
MAX_TITLE_CHARS = 200
UNTITLED = "(제목 없음)"
ARRIVED_LIMIT = 50  # 도착한 알림 목록 최대 개수
UPCOMING_LIMIT = 100  # 예정된 알림 목록 최대 개수

# 대화 리마인더 (ADR-0021 §5)
REMINDER_MAX_AHEAD = timedelta(days=365)
MAX_PENDING_REMINDERS = (
    100  # 워크스페이스당 예정된 대화 리마인더 상한(데이터 속 지시로 인한 남발 방지)
)

# 보존 (ADR-0021 §7): 일정 알림은 일정 시작 뒤, 대화 리마인더는 도착 뒤 이 기간이 지나면 지운다.
EVENT_REMINDER_RETENTION = timedelta(days=7)
REMINDER_RETENTION = timedelta(days=30)

NOTIFY_CONFIG: dict[str, int] = {
    "default_lead_minutes": DEFAULT_LEAD_MINUTES,
    "sync_window_minutes": int(SYNC_WINDOW.total_seconds() // 60),
    "max_title_chars": MAX_TITLE_CHARS,
    "reminder_max_ahead_days": REMINDER_MAX_AHEAD.days,
    "max_pending_reminders": MAX_PENDING_REMINDERS,
}
