from orbit.actions.provenance import classify_emails


def classify(
    emails: list[str],
    *,
    user: list[str] | None = None,
    workspace: list[str] | None = None,
    external: list[str] | None = None,
) -> dict[str, str]:
    return dict(
        classify_emails(
            emails,
            user_utterances=user or [],
            workspace_emails=workspace or [],
            external_texts=external or [],
        )
    )


def test_each_class() -> None:
    result = classify(
        ["u@example.com", "w@example.com", "x@example.com", "m@example.com"],
        user=["u@example.com 에게 보내줘"],
        workspace=["w@example.com"],
        external=["문의는 x@example.com 으로 주세요"],
    )
    assert result == {
        "u@example.com": "user",
        "w@example.com": "workspace_data",
        "x@example.com": "external_content",
        "m@example.com": "model",
    }


def test_priority_user_over_workspace_over_external() -> None:
    result = classify(
        ["a@example.com", "b@example.com"],
        user=["a@example.com"],
        workspace=["a@example.com", "b@example.com"],
        external=["a@example.com b@example.com"],
    )
    assert result == {"a@example.com": "user", "b@example.com": "workspace_data"}


def test_case_insensitive() -> None:
    result = classify(
        ["Alice@Example.com", "bob@example.com"],
        user=["alice@EXAMPLE.com"],
        external=["BOB@example.COM"],
    )
    assert result == {"alice@example.com": "user", "bob@example.com": "external_content"}


def test_word_boundary_in_text() -> None:
    result = classify(
        ["bob@example.com"],
        external=["xbob@example.com", "bob@example.com.evil.io", "bob@example.comx"],
    )
    assert result == {"bob@example.com": "model"}
    ok = classify(["bob@example.com"], external=["연락: (bob@example.com), 감사합니다."])
    assert ok == {"bob@example.com": "external_content"}
    end = classify(["bob@example.com"], external=["메일은 bob@example.com."])
    assert end == {"bob@example.com": "external_content"}


def test_user_utterance_also_uses_boundary() -> None:
    assert classify(["bob@example.com"], user=["xbob@example.com"]) == {"bob@example.com": "model"}


def test_duplicates_collapse() -> None:
    assert classify(["A@example.com", "a@example.com"]) == {"a@example.com": "model"}
