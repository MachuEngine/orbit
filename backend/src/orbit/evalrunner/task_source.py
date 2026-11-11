"""과제 데이터셋을 읽는 경계 (ADR-0009 §2, ADR-0010).

데이터셋 읽기와 형식·일관성 검증(`validate_dataset`, review.jsonl의 `exclude` 반영)은
`orbit.evaldata.dataset.load_dataset`가 맡는다. 러너는 그 결과(`orbit.evaldata.schema.Task`)를
그대로 쓰고, 이 모듈에서 러너에만 필요한 검증을 더한다.

러너 고유 검증: `required_tool_calls.args_subset`은 호출 기록 요약(`orbit.connectors.sim.calls`)과
비교하므로, 요약에서 값이 가려지거나 잘리는 키·문자열은 쓸 수 없다.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orbit.evaldata.dataset import DatasetError
from orbit.evaldata.dataset import load_dataset as _load_dataset
from orbit.evaldata.schema import Gold, Task

__all__ = ["SPLITS", "Dataset", "DatasetError", "GoldView", "TaskView", "load_dataset"]

SPLITS = ("dev", "test")

# 기존 이름 유지: 러너 코드는 이 별칭으로 과제·정답 타입을 가리킨다.
TaskView = Task
GoldView = Gold

# 호출 기록 요약(`orbit.connectors.sim.calls`)이 값을 가리는 규칙. 테스트가 원본과 같은지 확인한다.
REDACTED_KEYS = frozenset({"body_text", "description", "snippet"})
MAX_SUMMARY_STR = 80


@dataclass(frozen=True)
class Dataset:
    """러너가 쓰는 데이터셋. `world_snapshot`은 Sim 스냅샷 v1 dict다(`SimWorld.load`가 검증)."""

    name: str
    version: str
    manifest: Mapping[str, Any]
    world_snapshot: Mapping[str, Any]
    tasks: tuple[Task, ...]

    def tasks_for(self, split: str) -> tuple[Task, ...]:
        if split not in SPLITS:
            raise ValueError(f"split은 {SPLITS} 중 하나여야 한다: {split!r}")
        return tuple(t for t in self.tasks if t.split == split)


def _check_args_subset(value: Any, where: str) -> None:
    """호출 기록 요약과 비교할 수 없는 값(가려진 키, 80자 초과 문자열)이면 `DatasetError`."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k in REDACTED_KEYS:
                raise DatasetError(f"{where}: args_subset에 요약에서 가려지는 키 {k}를 쓸 수 없다")
            _check_args_subset(v, where)
    elif isinstance(value, list):
        for v in value:
            _check_args_subset(v, where)
    elif isinstance(value, str) and len(value) > MAX_SUMMARY_STR:
        raise DatasetError(f"{where}: args_subset 문자열이 {MAX_SUMMARY_STR}자를 넘는다")


def load_dataset(path: str | Path) -> Dataset:
    """`orbit.evaldata`로 읽고 검증한 뒤 러너 고유 검증을 한다. 검수에서 제외된 과제는 뺀다."""
    ds = _load_dataset(path)
    if not ds.tasks:
        raise DatasetError("과제가 없다(모두 제외됐거나 비어 있다)")
    for task in ds.tasks:
        for i, req in enumerate(task.gold.required_tool_calls):
            _check_args_subset(req.args_subset, f"{task.task_id}.required_tool_calls[{i}]")
    return Dataset(
        name=ds.manifest.name,
        version=ds.manifest.version,
        manifest=ds.manifest.model_dump(mode="json"),
        world_snapshot=ds.world_dict,
        tasks=tuple(ds.tasks),
    )
