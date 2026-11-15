"""Run 설정 지문 (D9-3). 프롬프트 버전과 도구 정의를 해시해 `run.config_fingerprint`에 남긴다."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from orbit.llm.stt import DEFAULT_STT_MODEL_ID
from orbit.mailindex.limits import DEFAULT_EMBEDDING_MODEL_ID, INDEX_CONFIG
from orbit.notify.limits import NOTIFY_CONFIG
from orbit.runflow.answer import (
    ANSWER_EXTENSION_VERSION,
    ANSWER_PROMPT_PARAGRAPH,
    answer_tool_spec,
)
from orbit.runflow.context import CONTEXT_CONFIG
from orbit.runflow.nodes import TEMPERATURE
from orbit.runflow.prompt import PROMPT_VERSION
from orbit.runflow.write_tools import run_tool_specs
from orbit.voice.limits import VOICE_CONFIG

PATTERN = "write-v0"


def config_fingerprint(
    model_id: str | None = None,
    llm_info: Mapping[str, Any] | None = None,
    embedding_model_id: str | None = None,
    stt_model_id: str | None = None,
    answer_extension: bool = False,
) -> str:
    """`model_id`는 고정 모델 ID(D9-3). 없거나 비어 있으면 "unset"으로 넣는다.

    `llm_info`(`llm_config_info`의 값)가 있으면 상수 temperature 대신 실제 LLM 설정(제공자,
    추론 강도, temperature 전송 여부, 단가표 등)을 지문에 넣는다.
    `embedding_model_id`(색인 임베딩 모델, ADR-0019)가 없으면 기본 모델 ID로 본다. 색인·검색 상수
    (`INDEX_CONFIG`)와 알림 상수(`NOTIFY_CONFIG`, ADR-0021)는 항상 지문에 들어간다.
    `stt_model_id`(음성 전사 모델, ADR-0025)가 없으면 기본 모델 ID로 본다.
    `answer_extension`(평가 전용, ADR-0023 §6)이 True면 답 제출 확장(버전·도구 정의·안내 문구)을
    지문에 더한다. 기본값(False)에서는 확장이 없던 때와 지문이 같다.
    """
    payload: dict[str, Any] = {
        "pattern": PATTERN,
        "model_id": model_id or "unset",
        "prompt_version": PROMPT_VERSION,
        "temperature": TEMPERATURE,
        "context": CONTEXT_CONFIG,
        "index": {
            **INDEX_CONFIG,
            "embedding_model_id": embedding_model_id or DEFAULT_EMBEDDING_MODEL_ID,
        },
        "notify": NOTIFY_CONFIG,
        "voice": {**VOICE_CONFIG, "stt_model_id": stt_model_id or DEFAULT_STT_MODEL_ID},
        "tools": [asdict(spec) for spec in run_tool_specs()],
    }
    if answer_extension:
        payload["answer_extension"] = {
            "version": ANSWER_EXTENSION_VERSION,
            "tool": asdict(answer_tool_spec()),
            "prompt": ANSWER_PROMPT_PARAGRAPH,
        }
    if llm_info is not None:
        del payload["temperature"]
        # 워커 경로는 provider_items를 저장하지 않아 추론 항목을 되돌리지 않는다.
        payload["llm"] = {**llm_info, "reasoning_items_roundtrip": False}
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
