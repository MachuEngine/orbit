"""설정 지문 (D9-3)."""

from orbit.runflow.fingerprint import config_fingerprint


def test_fingerprint_changes_with_model_id() -> None:
    a = config_fingerprint("vendor/model-a")
    assert a == config_fingerprint("vendor/model-a")
    assert a != config_fingerprint("vendor/model-b")
    assert config_fingerprint(None) == config_fingerprint("")  # 모델 미지정은 같은 값
    assert config_fingerprint(None) != a
