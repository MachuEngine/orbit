"""한국어 템플릿 문장. v0은 LLM 없이 템플릿으로만 본문과 질문을 만든다.

날짜·시각·이름 같은 사실은 호출하는 코드가 채운다. 이 모듈은 문장 틀만 가진다.
"""

# ruff: noqa: E501  (문장 템플릿 데이터라 줄 길이를 맞추지 않는다)
from __future__ import annotations

import random

MEETING_KINDS = ["설계 리뷰", "주간 싱크", "킥오프", "스프린트 회고", "데모 리허설", "기술 논의"]

GENERIC_TITLES = [
    "디자인 검토",
    "고객 인터뷰 준비",
    "분기 계획 점검",
    "채용 면접 조율",
    "예산 점검",
    "교육 자료 리뷰",
    "코드 리뷰 세션",
    "운영 현황 공유",
    "경쟁사 분석 토의",
    "온보딩 점검",
    "사내 세미나 준비",
    "성과 면담",
]

LOCATIONS = [
    "회의실 3층 A",
    "회의실 3층 B",
    "회의실 5층 대회의실",
    "화상 회의",
    "카페테리아 미팅룸",
]

DEADLINE_ITEMS = [
    "스프린트 산출물",
    "분기 보고서 초안",
    "예산 신청서",
    "릴리스 노트",
    "검토 의견서",
]

REASONS = [
    "담당자 일정이 겹쳐서",
    "안건이 다른 회의에서 정리돼서",
    "자료 준비가 늦어져서",
    "참석자가 부족해서",
]


def ro(when: str) -> str:
    """'오전 9시' -> '오전 9시로', '오전 9시 30분' -> '오전 9시 30분으로'."""
    return when + ("으로" if when.endswith("분") else "로")


def pick[T](rng: random.Random, items: list[T]) -> T:
    return items[rng.randrange(len(items))]


# ---------------------------------------------------------------- 메일


def invite(
    rng: random.Random, recipient: str, sender: str, title: str, when: str, location: str
) -> tuple[str, str]:
    subject = pick(
        rng, [f"[회의 초대] {title}", f"{title} 일정 안내", f"{title} 회의 초대드립니다"]
    )
    body = pick(
        rng,
        [
            f"{recipient}님, 안녕하세요. {sender}입니다.\n{title} 회의를 {when}에 진행하려고 합니다. "
            f"장소는 {location}입니다.\n참석 가능하신지 회신 부탁드립니다.\n감사합니다.\n{sender} 드림",
            f"안녕하세요, {recipient}님.\n{when}에 '{title}' 일정을 잡았습니다 ({location}). "
            f"캘린더 초대도 함께 보냈으니 확인해 주세요.\n{sender}",
            f"{recipient}님께\n{title} 논의를 위해 {when}에 모이면 좋겠습니다. 장소는 {location}입니다. "
            f"어려우시면 말씀해 주세요.\n{sender}",
        ],
    )
    return subject, body


def change_request(
    rng: random.Random, recipient: str, sender: str, title: str, old_when: str, new_when: str
) -> str:
    return pick(
        rng,
        [
            f"{recipient}님, 사정이 생겨 {title} 회의 시간을 {ro(new_when)} 옮기고 싶습니다. "
            f"기존 일정은 {old_when}에 잡혀 있었습니다. 괜찮으실까요?\n{sender}",
            f"안녕하세요, {recipient}님. {title} 일정이 바뀌었습니다. {old_when}에서 {ro(new_when)} "
            f"조정하겠습니다. 불편하시면 알려주세요.\n{sender}",
            f"{recipient}님, 죄송하지만 {title} 회의를 {new_when}에 하면 어떨까요? "
            f"({old_when}에는 어렵게 됐습니다.)\n{sender}",
        ],
    )


def confirm_change(
    rng: random.Random, recipient: str, sender: str, title: str, new_when: str
) -> str:
    return pick(
        rng,
        [
            f"{recipient}님, 확인했습니다. {title} 일정을 {ro(new_when)} 캘린더에 반영했습니다.\n{sender}",
            f"네, {new_when} 좋습니다. {title} 캘린더를 수정해 두었습니다.\n{sender}",
        ],
    )


def cancel_notice(
    rng: random.Random, recipient: str, sender: str, title: str, when: str, reason: str
) -> tuple[str, str]:
    subject = pick(rng, [f"[취소] {title}", f"{title} 취소 안내"])
    body = pick(
        rng,
        [
            f"{recipient}님, {when}에 예정이던 {title} 회의는 {reason} 취소합니다. "
            f"필요하면 다시 일정을 잡겠습니다.\n{sender}",
            f"안녕하세요, {recipient}님. {reason} {when} {title} 회의를 취소하게 됐습니다. "
            f"양해 부탁드립니다.\n{sender}",
        ],
    )
    return subject, body


def series_notice(rng: random.Random, recipient: str, sender: str, title: str, rule: str) -> str:
    return pick(
        rng,
        [
            f"{recipient}님, {title}를 {rule} 반복 일정으로 등록해 두었습니다. 캘린더를 확인해 주세요.\n{sender}",
            f"안녕하세요. {title}는 앞으로 {rule} 진행합니다. 반복 초대를 보냈습니다.\n{sender}",
        ],
    )


def deadline(rng: random.Random, project: str, item: str, due: str) -> tuple[str, str]:
    subject = pick(rng, [f"[마감 안내] {project} {item} 제출", f"{project} {item} 마감 공지"])
    body = pick(
        rng,
        [
            f"{project} {item} 제출 마감은 {due}입니다. 기한 안에 공유 폴더에 올려 주세요.",
            f"공지드립니다. {project} {item}는 {due}까지 제출해 주세요. 늦어지면 미리 알려 주시기 바랍니다.",
        ],
    )
    return subject, body


def followup(rng: random.Random, recipient: str, sender: str, title: str) -> tuple[str, str]:
    """회의 제목과 비슷하지만 시각을 담지 않는 후속 메일(비슷한 제목 노이즈)."""
    subject = pick(rng, [f"Re: {title} 준비 사항", f"{title} 참고 자료 공유", f"Re: {title} 안건"])
    body = pick(
        rng,
        [
            f"{recipient}님, {title} 전에 살펴보시면 좋을 자료를 공유드립니다. 의견 있으시면 남겨 주세요.\n{sender}",
            f"{recipient}님, {title} 안건에 한 가지 추가하고 싶은 항목이 있습니다. 회의 때 말씀드리겠습니다.\n{sender}",
        ],
    )
    return subject, body


def newsletter(rng: random.Random, number: int) -> tuple[str, str]:
    subject = pick(
        rng,
        [
            f"[주간 뉴스레터] 이번 주 개발 동향 {number}호",
            f"이번 주 업계 소식 모음 #{number}",
            f"[프로덕트 레터] {number}번째 이야기",
        ],
    )
    body = pick(
        rng,
        [
            "이번 주에 많이 읽힌 글을 정리했습니다. 팀 운영, 문서화, 배포 자동화에 관한 사례가 담겨 있습니다.",
            "새로 공개된 도구와 업데이트를 모았습니다. 구독을 원하지 않으시면 설정에서 해지할 수 있습니다.",
            "다음 달 열리는 온라인 모임 일정과 발표 주제를 소개합니다. 관심 있는 세션을 골라 보세요.",
        ],
    )
    return subject, body


def spam(rng: random.Random) -> tuple[str, str]:
    subject = pick(
        rng, ["[광고] 오늘만 특가 안내", "축하합니다! 경품 당첨 확인", "[광고] 회원 전용 할인"]
    )
    body = pick(
        rng,
        [
            "지금 확인하시면 추가 할인을 드립니다. 이 메일은 광고성 정보입니다.",
            "경품 수령을 위해 회신해 주세요. 기한이 지나면 무효가 됩니다.",
        ],
    )
    return subject, body


def notice(rng: random.Random) -> tuple[str, str]:
    subject = pick(
        rng, ["보안 알림: 새 기기에서 로그인했습니다", "월간 사용 내역 안내", "약관 변경 안내"]
    )
    body = pick(
        rng,
        [
            "회원님의 계정에 새 기기에서 로그인한 기록이 있습니다. 본인이 아니라면 비밀번호를 변경해 주세요.",
            "이번 달 사용 내역을 안내드립니다. 자세한 내용은 서비스 화면에서 확인할 수 있습니다.",
            "서비스 약관이 일부 변경됩니다. 변경 내용은 서비스 공지에서 확인해 주세요.",
        ],
    )
    return subject, body


# ---------------------------------------------------------------- 질문

DAY_QUESTIONS = [
    "{scope} 일정 알려줘.",
    "{scope} 일정이 어떻게 돼?",
    "{scope} 뭐 있는지 정리해 줘.",
    "{scope} 스케줄 좀 보여줘.",
]
CANCEL_QUESTIONS = DAY_QUESTIONS + ["{scope} 어떤 회의가 있어?"]
FREE_QUESTIONS = [
    "{scope} 가장 이른 1시간 빈 시간이 언제야? 업무 시간은 9시부터 18시까지로 봐줘.",
    "{scope} 한 시간짜리 미팅을 잡으려는데 가장 빨리 비는 시간이 언제야? (업무 시간 9시~18시 기준)",
]
RESCHEDULE_QUESTIONS = [
    "'{title}' 일정, 지금 캘린더 기준으로 언제야?",
    "{title} 시간이 바뀌었다고 들었어. 현재 잡혀 있는 날짜와 시간을 알려줘.",
    "{title} 회의 언제 하는지 최신 일정으로 알려줘.",
]
PERSON_QUESTIONS = [
    "{who}님과 다음 회의는 언제야?",
    "{who}님이랑 잡힌 가장 가까운 회의 날짜와 시간 알려줘.",
    "{who}님과의 다음 미팅 일정을 알려줘.",
]
PROJECT_QUESTIONS = [
    "{week} {project} 프로젝트 회의 일정을 알려줘.",
    "{week}에 {project} 관련 회의가 언제 있어?",
    "{week} {project} 회의 날짜랑 시간 정리해 줘.",
]
