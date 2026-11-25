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
from orbit.runflow.eval_variants import (
    FACTS_EXTENSION_VERSION,
    FACTS_PROMPT_PARAGRAPH,
    REASONING_ROUNDTRIP_VERSION,
)
from orbit.runflow.nodes import TEMPERATURE
from orbit.runflow.patterns import fingerprint_payload
from orbit.runflow.prompt import PROMPT_VERSION
from orbit.runflow.rag import fingerprint_payload as rag_payload
from orbit.runflow.write_tools import run_tool_specs
from orbit.voice.limits import VOICE_CONFIG

PATTERN = "write-v0"


def config_fingerprint(
    model_id: str | None = None,
    llm_info: Mapping[str, Any] | None = None,
    embedding_model_id: str | None = None,
    stt_model_id: str | None = None,
    answer_extension: bool = False,
    facts_extension: bool = False,
    reasoning_roundtrip: bool = False,
    agent_pattern: str | None = None,
    rag_pattern: str | None = None,
    reminder_approval: bool = True,
) -> str:
    """`model_id`는 고정 모델 ID(D9-3). 없거나 비어 있으면 "unset"으로 넣는다.

    `llm_info`(`llm_config_info`의 값)가 있으면 상수 temperature 대신 실제 LLM 설정(제공자,
    추론 강도, temperature 전송 여부, 단가표 등)을 지문에 넣는다.
    `embedding_model_id`(색인 임베딩 모델, ADR-0019)가 없으면 기본 모델 ID로 본다. 색인·검색 상수
    (`INDEX_CONFIG`)와 알림 상수(`NOTIFY_CONFIG`, ADR-0021)는 항상 지문에 들어간다.
    `stt_model_id`(음성 전사 모델, ADR-0025)가 없으면 기본 모델 ID로 본다.
    `answer_extension`(평가 전용, ADR-0023 §6)이 True면 답 제출 확장(버전·도구 정의·안내 문구)을
    지문에 더한다. 기본값(False)에서는 확장이 없던 때와 지문이 같다.
    `facts_extension`·`reasoning_roundtrip`(평가 전용, ADR-0023 §10)도 켰을 때만 버전과 문단을
    지문에 더한다. `reasoning_roundtrip`이면 `llm.reasoning_items_roundtrip`은 `llm_info`의 실제
    값(추론 강도 none이면 False)이다.
    `agent_pattern`(평가 전용, ADR-0030)이 `plan_execute`·`fixed_workflow`면 패턴 이름·버전·
    프롬프트·범주 표를 지문에 더한다. None이나 `react`면 더하지 않는다(현재 runflow와 지문이 같다).
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
        "tools": [asdict(spec) for spec in run_tool_specs(reminder_approval)],
    }
    if answer_extension:
        payload["answer_extension"] = {
            "version": ANSWER_EXTENSION_VERSION,
            "tool": asdict(answer_tool_spec()),
            "prompt": ANSWER_PROMPT_PARAGRAPH,
        }
    if facts_extension:
        payload["facts_extension"] = {
            "version": FACTS_EXTENSION_VERSION,
            "prompt": FACTS_PROMPT_PARAGRAPH,
        }
    if reasoning_roundtrip:
        payload["reasoning_roundtrip"] = {"version": REASONING_ROUNDTRIP_VERSION}
    if agent_pattern is not None and agent_pattern != "react":
        payload["agent_pattern"] = fingerprint_payload(agent_pattern)
    if rag_pattern is not None and rag_pattern != "agentic":
        payload["rag_pattern"] = rag_payload(rag_pattern)
    if llm_info is not None:
        del payload["temperature"]
        # 워커 경로는 provider_items를 저장하지 않아 추론 항목을 되돌리지 않는다. 평가 전용
        # `reasoning-rt`를 켜면 제공자 설정이 허용하는 만큼(llm_info 값) 되돌린다.
        roundtrip = (
            bool(llm_info.get("reasoning_items_roundtrip")) if reasoning_roundtrip else False
        )
        payload["llm"] = {**llm_info, "reasoning_items_roundtrip": roundtrip}
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
