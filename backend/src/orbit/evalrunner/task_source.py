"""과제 데이터셋을 읽는 경계 (ADR-0009 §2의 산출물 형식).

러너의 나머지 모듈은 이 모듈의 `TaskView`·`GoldView` Protocol만 안다. 데이터 생성기
(`orbit.evaldata.schema`)가 같은 속성 이름을 가진 모델을 내놓으면 아래 두 곳만 바꾸면 된다.

통합 시 바꿀 지점
1. `load_dataset`: `tasks.jsonl` 한 줄을 `_parse_task`로 파싱하는 부분을 `orbit.evaldata.schema`의
   과제 모델 검증(`model_validate`)으로 교체한다. 모델이 Protocol 속성(`as_of_time`은 tz-aware
   `datetime`, `turns`·`answer_facts` 등은 시퀀스)을 만족해야 한다.
2. 이 파일의 `*Record` 데이터클래스와 `_parse_*` 함수는 그때 지운다.
`manifest.json`·`world.json` 읽기와 검증(`Dataset`)은 evaldata 형식과 무관하게 남는다.

과제 한 줄의 형식:
    task_id, scenario_id, split("dev"|"test"), as_of_time(tz-aware ISO8601), account_id,
    turns[{role:"user", content}],
    gold{answer_facts[{fact_id, any_of[str], description}], must_not_contain[str],
         expected_changes(list[dict]|None), required_tool_calls[{tool, args_subset}],
         forbidden_tools[str]},
    difficulty{}, tags[]
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

SPLITS = ("dev", "test")

# 호출 기록 요약(`orbit.connectors.sim.calls`)이 값을 가리는 규칙. 테스트가 원본과 같은지 확인한다.
REDACTED_KEYS = frozenset({"body_text", "description", "snippet"})
MAX_SUMMARY_STR = 80


class DatasetError(ValueError):
    """데이터셋 파일을 읽을 수 없거나 형식이 계약과 다르다."""


# ---------------------------------------------------------------- 러너가 소비하는 Protocol


class TurnView(Protocol):
    @property
    def role(self) -> str: ...
    @property
    def content(self) -> str: ...


class AnswerFactView(Protocol):
    @property
    def fact_id(self) -> str: ...
    @property
    def any_of(self) -> Sequence[str]: ...
    @property
    def description(self) -> str: ...


class RequiredToolCallView(Protocol):
    @property
    def tool(self) -> str: ...
    @property
    def args_subset(self) -> Mapping[str, Any]: ...


class ExpectedAnswerView(Protocol):
    @property
    def kind(self) -> str: ...
    @property
    def events(self) -> Sequence["EventRef"]: ...
    @property
    def event(self) -> "EventRef | None": ...
    @property
    def start(self) -> datetime | None: ...


class GoldView(Protocol):
    @property
    def answer_facts(self) -> Sequence[AnswerFactView]: ...
    @property
    def must_not_contain(self) -> Sequence[str]: ...
    @property
    def expected_changes(self) -> Sequence[Mapping[str, Any]] | None: ...
    @property
    def required_tool_calls(self) -> Sequence[RequiredToolCallView]: ...
    @property
    def forbidden_tools(self) -> Sequence[str]: ...
    @property
    def expected_answer(self) -> "ExpectedAnswer | None": ...


class TaskView(Protocol):
    @property
    def task_id(self) -> str: ...
    @property
    def scenario_id(self) -> str: ...
    @property
    def split(self) -> str: ...
    @property
    def as_of_time(self) -> datetime: ...
    @property
    def account_id(self) -> str: ...
    @property
    def turns(self) -> Sequence[TurnView]: ...
    @property
    def gold(self) -> GoldView: ...
    @property
    def difficulty(self) -> Mapping[str, Any]: ...
    @property
    def tags(self) -> Sequence[str]: ...


# ---------------------------------------------------------------- JSON 직접 파싱 구현(임시)


@dataclass(frozen=True)
class TurnRecord:
    role: str
    content: str


@dataclass(frozen=True)
class FactRecord:
    fact_id: str
    any_of: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class RequiredCallRecord:
    tool: str
    args_subset: Mapping[str, Any]


@dataclass(frozen=True)
class EventRef:
    calendar_id: str
    event_id: str


@dataclass(frozen=True)
class ExpectedAnswer:
    """구조화된 정답 (ADR-0010 §1). kind별로 쓰는 필드만 채운다."""

    kind: str  # event_set | event | event_time | time_slot
    events: tuple[EventRef, ...] = ()
    event: EventRef | None = None
    start: datetime | None = None  # tz-aware


@dataclass(frozen=True)
class GoldRecord:
    answer_facts: tuple[FactRecord, ...]
    must_not_contain: tuple[str, ...]
    expected_changes: tuple[Mapping[str, Any], ...] | None
    required_tool_calls: tuple[RequiredCallRecord, ...]
    forbidden_tools: tuple[str, ...]
    expected_answer: ExpectedAnswer | None = None


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    scenario_id: str
    split: str
    as_of_time: datetime
    account_id: str
    turns: tuple[TurnRecord, ...]
    gold: GoldRecord
    difficulty: Mapping[str, Any]
    tags: tuple[str, ...]


def _get[T](data: Mapping[str, Any], key: str, kind: type[T], where: str) -> T:
    if key not in data:
        raise DatasetError(f"{where}: '{key}' 필드가 없다")
    value = data[key]
    if not isinstance(value, kind):
        raise DatasetError(f"{where}: '{key}'의 형식이 {kind.__name__}가 아니다")
    return value


def _str_list(data: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    items = _get(data, key, list, where)
    if not all(isinstance(i, str) for i in items):
        raise DatasetError(f"{where}: '{key}'는 문자열 목록이어야 한다")
    return tuple(items)


def _check_args_subset(value: Any, where: str, key: str = "") -> None:
    """호출 기록 요약과 비교할 수 없는 값(가려진 키, 80자 초과 문자열)이면 `DatasetError`."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k in REDACTED_KEYS:
                raise DatasetError(f"{where}: args_subset에 요약에서 가려지는 키 {k}를 쓸 수 없다")
            _check_args_subset(v, where, str(k))
    elif isinstance(value, list):
        for v in value:
            _check_args_subset(v, where, key)
    elif isinstance(value, str) and len(value) > MAX_SUMMARY_STR:
        raise DatasetError(f"{where}: args_subset 문자열이 {MAX_SUMMARY_STR}자를 넘는다")


def _parse_event(value: Any, where: str) -> EventRef:
    if not isinstance(value, dict) or set(value) != {"calendar_id", "event_id"}:
        raise DatasetError(f"{where}: 일정은 calendar_id, event_id만 가진 객체여야 한다")
    cal, ev = value["calendar_id"], value["event_id"]
    if not isinstance(cal, str) or not isinstance(ev, str) or not cal or not ev:
        raise DatasetError(f"{where}: calendar_id, event_id는 비어 있지 않은 문자열이어야 한다")
    return EventRef(cal, ev)


def _parse_start(value: Any, where: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DatasetError(f"{where}: start는 시간대 오프셋이 있는 ISO 8601이어야 한다")
    return parsed


_ANSWER_KEYS = {
    "event_set": {"kind", "events"},
    "event": {"kind", "event"},
    "event_time": {"kind", "event", "start"},
    "time_slot": {"kind", "start"},
}


def _parse_expected_answer(data: Any, where: str) -> ExpectedAnswer | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise DatasetError(f"{where}: expected_answer는 객체 또는 null이어야 한다")
    kind = data.get("kind")
    if kind not in _ANSWER_KEYS:
        raise DatasetError(f"{where}: expected_answer.kind는 {sorted(_ANSWER_KEYS)} 중 하나다")
    if set(data) != _ANSWER_KEYS[kind]:
        raise DatasetError(
            f"{where}: expected_answer({kind})의 키가 {sorted(_ANSWER_KEYS[kind])}와 다르다"
        )
    events: tuple[EventRef, ...] = ()
    if kind == "event_set":
        raw = data["events"]
        if not isinstance(raw, list):
            raise DatasetError(f"{where}: events는 목록이어야 한다")
        events = tuple(_parse_event(e, f"{where}.events") for e in raw)
        if len(set(events)) != len(events):
            raise DatasetError(f"{where}: events에 중복 항목이 있다")
    event = _parse_event(data["event"], f"{where}.event") if "event" in data else None
    start = _parse_start(data["start"], where) if "start" in data else None
    return ExpectedAnswer(kind, events, event, start)


def _parse_gold(data: Mapping[str, Any], where: str) -> GoldRecord:
    facts: list[FactRecord] = []
    for i, f in enumerate(_get(data, "answer_facts", list, where)):
        w = f"{where}.answer_facts[{i}]"
        if not isinstance(f, dict):
            raise DatasetError(f"{w}: 객체가 아니다")
        any_of = _str_list(f, "any_of", w)
        if not any_of or any(not s.strip() for s in any_of):
            raise DatasetError(f"{w}: any_of는 비어 있지 않은 문자열이 하나 이상 있어야 한다")
        facts.append(FactRecord(_get(f, "fact_id", str, w), any_of, _get(f, "description", str, w)))
    must_not = _str_list(data, "must_not_contain", where)
    if any(not s.strip() for s in must_not):
        raise DatasetError(f"{where}: must_not_contain에 빈 문자열이 있다")
    raw_changes = data.get("expected_changes", None)
    changes: tuple[Mapping[str, Any], ...] | None
    if raw_changes is None:
        changes = None
    elif isinstance(raw_changes, list) and all(isinstance(c, dict) for c in raw_changes):
        changes = tuple(raw_changes)
    else:
        raise DatasetError(f"{where}: expected_changes는 객체 목록 또는 null이어야 한다")
    calls: list[RequiredCallRecord] = []
    for i, c in enumerate(_get(data, "required_tool_calls", list, where)):
        w = f"{where}.required_tool_calls[{i}]"
        if not isinstance(c, dict):
            raise DatasetError(f"{w}: 객체가 아니다")
        subset = _get(c, "args_subset", dict, w)
        _check_args_subset(subset, w)
        calls.append(RequiredCallRecord(_get(c, "tool", str, w), subset))
    return GoldRecord(
        answer_facts=tuple(facts),
        must_not_contain=must_not,
        expected_changes=changes,
        required_tool_calls=tuple(calls),
        forbidden_tools=_str_list(data, "forbidden_tools", where),
        expected_answer=_parse_expected_answer(
            data.get("expected_answer"), f"{where}.expected_answer"
        ),
    )


def _parse_task(data: Any, where: str) -> TaskRecord:
    if not isinstance(data, dict):
        raise DatasetError(f"{where}: 객체가 아니다")
    split = _get(data, "split", str, where)
    if split not in SPLITS:
        raise DatasetError(f"{where}: split은 {SPLITS} 중 하나여야 한다")
    raw_time = _get(data, "as_of_time", str, where)
    try:
        as_of = datetime.fromisoformat(raw_time)
    except ValueError as exc:
        raise DatasetError(f"{where}: as_of_time을 읽을 수 없다") from exc
    if as_of.tzinfo is None:
        raise DatasetError(f"{where}: as_of_time에 시간대가 없다")
    turns: list[TurnRecord] = []
    for i, t in enumerate(_get(data, "turns", list, where)):
        w = f"{where}.turns[{i}]"
        if not isinstance(t, dict):
            raise DatasetError(f"{w}: 객체가 아니다")
        turns.append(TurnRecord(_get(t, "role", str, w), _get(t, "content", str, w)))
    if not turns:
        raise DatasetError(f"{where}: turns가 비었다")
    return TaskRecord(
        task_id=_get(data, "task_id", str, where),
        scenario_id=_get(data, "scenario_id", str, where),
        split=split,
        as_of_time=as_of,
        account_id=_get(data, "account_id", str, where),
        turns=tuple(turns),
        gold=_parse_gold(_get(data, "gold", dict, where), f"{where}.gold"),
        difficulty=_get(data, "difficulty", dict, where),
        tags=_str_list(data, "tags", where),
    )


# ---------------------------------------------------------------- 데이터셋


@dataclass(frozen=True)
class Dataset:
    """데이터셋 디렉터리 하나. `world_snapshot`은 Sim 스냅샷 v1 dict다(`SimWorld.load`가 검증)."""

    name: str
    version: str
    manifest: Mapping[str, Any]
    world_snapshot: Mapping[str, Any]
    tasks: tuple[TaskView, ...]

    def tasks_for(self, split: str) -> tuple[TaskView, ...]:
        if split not in SPLITS:
            raise ValueError(f"split은 {SPLITS} 중 하나여야 한다: {split!r}")
        return tuple(t for t in self.tasks if t.split == split)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError(f"{path.name}을 읽을 수 없다: {exc}") from exc


def load_dataset(path: str | Path) -> Dataset:
    root = Path(path)
    manifest = _read_json(root / "manifest.json")
    if not isinstance(manifest, dict):
        raise DatasetError("manifest.json 최상위는 객체여야 한다")
    name = _get(manifest, "name", str, "manifest")
    version = _get(manifest, "version", str, "manifest")
    world = _read_json(root / "world.json")
    if not isinstance(world, dict):
        raise DatasetError("world.json 최상위는 객체여야 한다")
    accounts = world.get("accounts")
    if not isinstance(accounts, dict):
        raise DatasetError("world.json에 accounts가 없다")

    tasks: list[TaskView] = []
    seen: set[str] = set()
    try:
        lines = (root / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DatasetError(f"tasks.jsonl을 읽을 수 없다: {exc}") from exc
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        where = f"tasks.jsonl:{n}"
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{where}: JSON이 아니다") from exc
        task = _parse_task(raw, where)
        if task.task_id in seen:
            raise DatasetError(f"{where}: task_id 중복 {task.task_id}")
        seen.add(task.task_id)
        if task.account_id not in accounts:
            raise DatasetError(f"{where}: world.json에 없는 계정 {task.account_id}")
        tasks.append(task)
    if not tasks:
        raise DatasetError("tasks.jsonl에 과제가 없다")
    return Dataset(name, version, manifest, world, tuple(tasks))
