"""Run 설정 지문 (D9-3). 프롬프트 버전과 도구 정의를 해시해 `run.config_fingerprint`에 남긴다."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from orbit.runflow.context import CONTEXT_CONFIG
from orbit.runflow.nodes import TEMPERATURE
from orbit.runflow.prompt import PROMPT_VERSION
from orbit.runflow.write_tools import run_tool_specs

PATTERN = "write-v0"


def config_fingerprint(
    model_id: str | None = None, llm_info: Mapping[str, Any] | None = None
) -> str:
    """`model_id`는 고정 모델 ID(D9-3). 없거나 비어 있으면 "unset"으로 넣는다.

    `llm_info`(`llm_config_info`의 값)가 있으면 상수 temperature 대신 실제 LLM 설정(제공자,
    추론 강도, temperature 전송 여부, 단가표 등)을 지문에 넣는다. 없으면 기존 지문과 같다.
    """
    payload: dict[str, Any] = {
        "pattern": PATTERN,
        "model_id": model_id or "unset",
        "prompt_version": PROMPT_VERSION,
        "temperature": TEMPERATURE,
        "context": CONTEXT_CONFIG,
        "tools": [asdict(spec) for spec in run_tool_specs()],
    }
    if llm_info is not None:
        del payload["temperature"]
        # 워커 경로는 provider_items를 저장하지 않아 추론 항목을 되돌리지 않는다.
        payload["llm"] = {**llm_info, "reasoning_items_roundtrip": False}
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
