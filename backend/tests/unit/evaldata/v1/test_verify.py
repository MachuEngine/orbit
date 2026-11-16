"""검증 규칙 단위 테스트: 정상 뼈대를 일부러 망가뜨리면 검증이 실패해야 한다."""

import copy
import re
from collections.abc import Callable
from typing import Any

import pytest

from orbit.evaldata.v1.skeleton import Skeleton
from orbit.evaldata.v1.verify import SkeletonError, verify_skeleton
from orbit.evaldata.v1.worldmodel import WorldModel

Mutation = Callable[[dict[str, Any]], None]


def _dump(sk: Skeleton) -> dict[str, Any]:
    return copy.deepcopy(sk.model_dump(mode="json", by_alias=True))


def _scn(d: dict[str, Any], type_: str, variant: str | None = None) -> dict[str, Any]:
    for s in d["scenarios"]:
        if s["type"] == type_ and (variant is None or s["truth"].get("variant") == variant):
            return s  # type: ignore[no-any-return]
    raise AssertionError(type_)


def _expect(wm: WorldModel, sk: Skeleton, mutate: Mutation, pattern: str) -> None:
    d = _dump(sk)
    mutate(d)
    with pytest.raises(SkeletonError) as info:
        verify_skeleton(wm, Skeleton.model_validate(d))
    assert any(re.search(pattern, e) for e in info.value.errors), info.value.errors[:5]


def test_clean_skeleton_passes(small_wm: WorldModel, small_sk: Skeleton) -> None:
    verify_skeleton(small_wm, small_sk)


def test_missing_required_fact_in_template(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        for b in d["bodies"].values():
            if b["required_facts"]:
                b["template_text"] = "내용 없음"
                return

    _expect(small_wm, small_sk, m, "필수 사실")


def test_forbidden_fact_in_template(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        for b in d["bodies"].values():
            if b["forbidden_facts"]:
                b["template_text"] += " " + b["forbidden_facts"][0]["surface_forms"][0]
                return

    _expect(small_wm, small_sk, m, "금지 사실")


def test_overlapping_required_and_forbidden_forms(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        for b in d["bodies"].values():
            if b["required_facts"] and b["forbidden_facts"]:
                b["forbidden_facts"][0]["surface_forms"] = [
                    b["required_facts"][0]["surface_forms"][0] + " 추가"
                ]
                return

    _expect(small_wm, small_sk, m, "표기가 겹친다")


def test_shared_evidence_between_scenarios(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        a = [s for s in d["scenarios"] if s["type"] == "day_agenda"]
        a[1]["evidence"].append(a[0]["evidence"][0])
        a[1]["account_id"] = a[0]["account_id"]

    _expect(small_wm, small_sk, m, "공유")


def test_dangling_reference(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        _scn(d, "reschedule")["evidence"][0]["id"] = "nope"

    _expect(small_wm, small_sk, m, "가 없다")


def test_absent_person_with_meeting(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        s = _scn(d, "absent", "person_no_meeting")
        ev = d["accounts"][s["account_id"]]["events"][0]
        ev["attendees"].append(
            {"email": s["truth"]["person_email"], "name": "x", "response_status": "accepted"}
        )

    _expect(small_wm, small_sk, m, "없어야 할 인물")


def test_absent_document_exists(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        s = _scn(d, "absent", "no_document")
        d["accounts"][s["account_id"]]["files"][0]["name"] = s["truth"]["query"]

    _expect(small_wm, small_sk, m, "없어야 할 문서")


def test_absent_amount_exists(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        s = _scn(d, "absent", "no_amount")
        msg = d["accounts"][s["account_id"]]["messages"][0]
        body = d["bodies"][msg["body_id"]]
        body["context"]["project"] = s["truth"]["query"]
        body["required_facts"].append(
            {"fact_id": "a", "kind": "amount", "value": 1, "surface_forms": ["1원"]}
        )
        body["template_text"] += " 1원"

    _expect(small_wm, small_sk, m, "금액·일정 필수 사실")


def test_absent_project_meeting_exists(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        s = _scn(d, "absent", "project_no_meeting")
        d["accounts"][s["account_id"]]["events"][0]["title"] = s["truth"]["query"] + " 점검"

    _expect(small_wm, small_sk, m, "미팅이 없어야 할")


def test_ambiguous_interpretations_equal(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        t = _scn(d, "ambiguous", "person_meetings")["truth"]
        t["interpretations"][1]["gold"] = t["interpretations"][0]["gold"]

    _expect(small_wm, small_sk, m, "정답이 같")


def test_day_agenda_truth_mismatch(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        _scn(d, "day_agenda")["truth"]["events"].pop()

    _expect(small_wm, small_sk, m, "day_agenda 일정이 세계와 다르다")


def test_person_next_truth_mismatch(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        t = _scn(d, "person_next")["truth"]
        t["upcoming"].reverse()

    _expect(small_wm, small_sk, m, "person_next")


def test_real_domain_and_phone_number(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def dom(d: dict[str, Any]) -> None:
        d["accounts"]["acct-u1"]["messages"][0]["from"]["email"] = "someone@gmail.com"

    _expect(small_wm, small_sk, dom, "도메인")

    def phone(d: dict[str, Any]) -> None:
        next(iter(d["bodies"].values()))["template_text"] += " 연락처 010-1234-5678"

    _expect(small_wm, small_sk, phone, "전화번호")


def test_length_ratio_out_of_tolerance(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        for b in d["bodies"].values():
            if b["artifact"] == "mail":
                b["length_class"], b["target_chars"] = "short", [60, 200]

    _expect(small_wm, small_sk, m, "길이 비율")


def test_family_mismatch_between_scenario_bodies(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        s = _scn(d, "mail_fact")
        bid = s["evidence"][0]["body_id"]
        other = "google" if d["bodies"][bid]["generator_family"] != "google" else "openai"
        d["bodies"][bid]["generator_family"] = other

    _expect(small_wm, small_sk, m, "계열")


def test_all_day_event_not_at_utc_midnight(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        for a in d["accounts"].values():
            for e in a["events"]:
                if e["all_day"]:
                    e["start"] = e["start"].replace("T00:00:00", "T01:00:00")
                    return

    _expect(small_wm, small_sk, m, "UTC 00:00")


def test_search_mailbox_not_subset(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        a = d["accounts"]["acct-s20"]
        a["messages"][0]["body_id"] = next(iter(d["bodies"]))
        d["search_mailboxes"]["acct-s20"]["message_by_body"] = {
            x["body_id"]: x["id"] for x in a["messages"]
        }

    _expect(small_wm, small_sk, m, "부분집합|본문 명세")


def test_message_count_outside_target(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        a = d["accounts"]["acct-u1"]
        a["messages"] = a["messages"][:50]

    _expect(small_wm, small_sk, m, "메일 수")


# ---------------------------------------------------------------- 질문 키·조용한 프로젝트


def test_duplicate_question_key(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        facts = [s for s in d["scenarios"] if s["type"] == "mail_fact"]
        a = facts[0]
        b = next(s for s in facts[1:] if s["account_id"] == a["account_id"])
        b["truth"]["question_key"] = a["truth"]["question_key"]

    _expect(small_wm, small_sk, m, "질문 키")


def test_deadline_shares_key_space_with_mail_fact(small_wm: WorldModel, small_sk: Skeleton) -> None:
    keys = {
        (s.account_id, str(s.truth.model_dump()["question_key"]))
        for s in small_sk.scenarios
        if s.type in ("mail_fact", "deadline")
    }
    n = sum(1 for s in small_sk.scenarios if s.type in ("mail_fact", "deadline"))
    assert len(keys) == n


def test_quiet_project_body_without_promise_ban(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        s = _scn(d, "absent", "no_amount")
        for b in d["bodies"].values():
            if b["context"]["project"] == s["truth"]["query"]:
                b["forbidden_facts"] = []

    _expect(small_wm, small_sk, m, "약속 금지")


def test_background_body_mentions_quiet_project(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        s = _scn(d, "absent", "project_no_meeting")
        for msg in d["accounts"][s["account_id"]]["messages"]:
            body = d["bodies"][msg["body_id"]]
            if not body["required_facts"] and body["context"]["project"] is None:
                body["context"]["topic"] = s["truth"]["query"] + " 이야기"
                return

    _expect(small_wm, small_sk, m, "약속 금지")


# ---------------------------------------------------------------- 재계산하지 않는 유형


def test_reschedule_new_time_differs_from_event(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        _scn(d, "reschedule")["truth"]["new"]["start"] = "2026-12-20T01:00:00Z"

    _expect(small_wm, small_sk, m, "reschedule")


def test_reschedule_old_time_before_change_mail(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        _scn(d, "reschedule")["truth"]["old"]["start"] = "2026-09-20T01:00:00Z"

    _expect(small_wm, small_sk, m, "변경 메일이 옛 일정 시각보다 늦다")


def test_cancellation_event_not_cancelled(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        s = _scn(d, "cancellation")
        ref = s["truth"]["event"]
        for e in d["accounts"][s["account_id"]]["events"]:
            if e["id"] == ref["id"]:
                e["status"] = "confirmed"

    _expect(small_wm, small_sk, m, "취소 상태")


def test_mismatch_calendar_values(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        _scn(d, "mismatch")["truth"]["calendar_location"] = "없는 장소"

    _expect(small_wm, small_sk, m, "mismatch")


def test_cross_midnight_span(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        _scn(d, "cross_midnight")["truth"]["span"]["end"] = "2026-12-20T01:00:00Z"

    _expect(small_wm, small_sk, m, "cross_midnight")


def test_all_day_span(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        _scn(d, "all_day")["truth"]["span"]["start"] = "2026-12-20T00:00:00Z"

    _expect(small_wm, small_sk, m, "all_day")


def test_tz_event_other_zone_time(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        _scn(d, "tz_event")["truth"]["start_in_other_zone"] = "2026-12-20T01:00:00+09:00"

    _expect(small_wm, small_sk, m, "tz_event")


def test_mail_fact_latest_equals_previous(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        t = _scn(d, "mail_fact")["truth"]
        t["previous"]["value"] = t["latest"]["value"]

    _expect(small_wm, small_sk, m, "같다")


def test_mail_fact_latest_not_in_latest_body(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        _scn(d, "mail_fact")["truth"]["latest"]["value"] = "엉뚱한 값"

    _expect(small_wm, small_sk, m, "최신 값이 최신 아티팩트")


def test_deadline_value_mismatch(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        _scn(d, "deadline")["truth"]["days_until"] += 3

    _expect(small_wm, small_sk, m, "days_until")


def test_doc_version_previous_not_in_previous_file(
    small_wm: WorldModel, small_sk: Skeleton
) -> None:
    def m(d: dict[str, Any]) -> None:
        _scn(d, "doc_version")["truth"]["previous"]["value"] = 1

    _expect(small_wm, small_sk, m, "이전 값이 이전 아티팩트")


# ---------------------------------------------------------------- 검색 메일함 규칙


def test_search_relevant_body_missing_from_smallest_mailbox(
    small_wm: WorldModel, small_sk: Skeleton
) -> None:
    def m(d: dict[str, Any]) -> None:
        small = {x["body_id"] for x in d["accounts"]["acct-s20"]["messages"]}
        small |= {f["body_id"] for f in d["accounts"]["acct-s20"]["files"]}
        outside = next(
            x["body_id"]
            for x in d["accounts"]["acct-s150"]["messages"]
            if x["body_id"] not in small
        )
        _scn(d, "search_topic")["truth"]["relevance_by_body"][outside] = 2

    _expect(small_wm, small_sk, m, "가장 작은 메일함에 없다")


def test_search_mailbox_size_wrong(small_wm: WorldModel, small_sk: Skeleton) -> None:
    def m(d: dict[str, Any]) -> None:
        d["search_mailboxes"]["acct-s60"]["size"] += 1

    _expect(small_wm, small_sk, m, "search_mailboxes")
