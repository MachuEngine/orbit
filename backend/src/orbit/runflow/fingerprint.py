"""Run 설정 지문 (D9-3). 프롬프트 버전과 도구 정의를 해시해 `run.config_fingerprint`에 남긴다."""

import hashlib
import json
from dataclasses import asdict

from orbit.runflow.nodes import TEMPERATURE
from orbit.runflow.prompt import PROMPT_VERSION
from orbit.runflow.write_tools import run_tool_specs

PATTERN = "write-v0"


def config_fingerprint() -> str:
    payload = {
        "pattern": PATTERN,
        "prompt_version": PROMPT_VERSION,
        "temperature": TEMPERATURE,
        "tools": [asdict(spec) for spec in run_tool_specs()],
    }
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
