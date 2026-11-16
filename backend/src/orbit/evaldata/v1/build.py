"""Sim 세계 v1 구조 생성기 본체와 CLI (ADR-0026 §2·§3).

    python -m orbit.evaldata.v1.build --world eval/worlds/orbit-v1.toml --seed 20261116 \\
        --out /tmp/world-v1 [--template-world]

산출물: `<out>/skeleton.json`, `<out>/skeleton-stats.json`, (`--template-world`면)
`<out>/world.template.json`. 같은 세계 모델·시드·git SHA면 `skeleton.json`이 바이트 단위로 같다.
LLM과 네트워크를 쓰지 않는다. 검증을 통과하지 못하면 생성을 실패시킨다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orbit.evaldata.generate import git_sha
from orbit.evaldata.v1 import background
from orbit.evaldata.v1 import scen_calendar as sc
from orbit.evaldata.v1 import scen_docs as sd
from orbit.evaldata.v1 import scen_mail as sm
from orbit.evaldata.v1.core import BodyDraft, Builder, Scn, WorldBuildError
from orbit.evaldata.v1.plan import allocate_claims, plan_scenarios
from orbit.evaldata.v1.skeleton import (
    SCHEMA_VERSION,
    TARGET_CHARS,
    BodySpec,
    Family,
    ScenarioRecord,
    SearchMailbox,
    SkAccount,
    Skeleton,
    SkPerson,
    SkThread,
)
from orbit.evaldata.v1.worldmodel import WorldModel, load_world_model

SCENARIO_FUNCS: dict[str, Callable[[Builder, Scn], None]] = {
    "day_agenda": sc.day_agenda,
    "free_slot": sc.free_slot,
    "reschedule": sc.reschedule,
    "cancellation": sc.cancellation,
    "project_week": sc.project_week,
    "person_next": sc.person_next,
    "cross_midnight": sc.cross_midnight,
    "tz_event": sc.tz_event,
    "all_day": sc.all_day,
    "mail_fact": sm.mail_fact,
    "reply_needed": sm.reply_needed,
    "meeting_request": sm.meeting_request,
    "event_change_target": sm.event_change_target,
    "doc_fact": sd.doc_fact,
    "doc_version": sd.doc_version,
    "deadline": sm.deadline,
    "mismatch": sm.mismatch,
    "absent": sd.absent,
    "ambiguous": sd.ambiguous,
    "search_topic": sd.search_topic,
}
# 장르별 길이 성향(0=짧게, 1=길게). 전체 비율은 분위수로 맞춘다.
_LENGTH_BIAS = {
    "notice": 0.15,
    "spam": 0.2,
    "colleague": 0.4,
    "customer": 0.5,
    "vendor": 0.5,
    "newsletter": 0.85,
}
DOC_MEDIUM_RATIO = 0.6

NOTES = [
    "본문은 비어 있다. bodies[*].template_text는 본문 실현 실패 때 쓰는 결정적 대체 본문이다.",
    "종일 일정은 계약 해석(ADR-0027)대로 캘린더 시간대의 00:00~다음 날 00:00인 순간으로 만들었다. "
    "일광 절약 시간 전환일은 24시간이 아닐 수 있다.",
    "시나리오의 as_of는 사용자 현지 평일의 제안 시각이다. 과제 생성기가 바꿔도 된다.",
    "검색 사용자 세 명은 같은 인물 설정을 공유하고 같은 body_id를 쓴다. "
    "작은 메일함은 큰 메일함의 부분집합이다.",
    "일정은 UTC로 저장한다. 현지 날짜 계산은 계정 time_zone을 쓴다.",
]


def _scenario_funcs(type_: str) -> Callable[[Builder, Scn], None]:
    return SCENARIO_FUNCS[type_]


def run_scenarios(b: Builder) -> None:
    items = plan_scenarios(b.wm)
    allocate_claims(b, items)
    accts = {u.key: u.account_id for u in b.wm.users}
    for it in items:
        s = b.begin(it.type, accts[it.user_key], it.variant)
        _scenario_funcs(it.type)(b, s)
        if s.truth is None:
            raise WorldBuildError(f"{s.scenario_id}: 정답 재료가 없다")


def fill_background(b: Builder) -> None:
    t = b.wm.targets
    for user in b.wm.e2e_users():
        acct = user.account_id
        background.add_recurring(b, user)
        background.add_background_events(b, user)
        rng = b.rng(f"bgmail:{user.key}")
        need = t.mails_per_e2e_user - len(b.acc[acct].messages)
        if need < 0:
            raise WorldBuildError(f"{user.key}: 시나리오 메일이 목표보다 많다({-need}통 초과)")
        background.fill_mail(b, [acct], need, rng=rng)
        drng = b.rng(f"bgdocs:{user.key}")
        want = drng.randint(*t.docs_per_e2e_user)
        have = len(b.acc[acct].files)
        if have > t.docs_per_e2e_user[1]:
            raise WorldBuildError(f"{user.key}: 시나리오 문서가 상한보다 많다({have})")
        background.fill_docs(b, [acct], max(0, want - have), rng=drng)
    # 검색 사용자: 그룹(0=200통 메일함, 1=1,000통에 추가, 2=5,000통에 추가)별로 채운다
    search = b.search_accts
    sizes = t.search_sizes
    for g, acct_g in enumerate(search):
        add = sizes[g] - (sizes[g - 1] if g else 0)
        have = sum(1 for gid in b.mail_group_of_body.values() if gid == g)
        if have > add:
            raise WorldBuildError(
                f"검색 메일함 그룹 {g}: 시나리오 메일 {have}통이 {add}통을 넘는다"
            )
        background.fill_mail(
            b, search[g:], add - have, rng=b.rng(f"bgsearch:{g}:{acct_g}"), group=g
        )
    big = search[-1]
    drng = b.rng("bgdocs:search")
    need_docs = t.search_docs - len(b.acc[big].files)
    if need_docs < 0:
        raise WorldBuildError("검색 문서가 search_docs보다 많다")
    background.fill_docs(b, search, need_docs, rng=drng)


# ---------------------------------------------------------------- 본문 명세 확정


def _assign_lengths(b: Builder, drafts: list[BodyDraft]) -> dict[str, str]:
    rng = b.rng("lengths")
    mix = b.wm.targets.length_mix
    out: dict[str, str] = {}
    for artifact in ("mail", "document"):
        items = sorted((d for d in drafts if d.artifact == artifact), key=lambda d: d.body_id)
        if not items:
            continue
        scored = []
        for d in items:
            bias = _LENGTH_BIAS.get(d.genre, 0.5) if artifact == "mail" else 0.5
            scored.append((bias + rng.uniform(-0.3, 0.3), d.body_id))
        scored.sort()
        n = len(scored)
        if artifact == "mail":
            n_short = round(mix.short * n)
            n_medium = round(mix.medium * n)
        else:
            n_short, n_medium = 0, round(DOC_MEDIUM_RATIO * n)
        for i, (_, bid) in enumerate(scored):
            if i < n_short:
                out[bid] = "short"
            elif i < n_short + n_medium:
                out[bid] = "medium"
            else:
                out[bid] = "long"
    return out


def _balance_by_swaps(
    fams: list[Family],
    by_type: dict[str, list[Scn]],
    bodies_of: dict[str, list[BodyDraft]],
    lengths: dict[str, str],
    fam_of_scn: dict[str, Family],
) -> None:
    """같은 유형의 두 시나리오가 계열을 맞바꿔 장르·길이별 계열 편차를 줄인다(유형별 수는 그대로).

    목적함수는 (장르·길이 키) x (계열)의 개수 편차 제곱합이다. 개선이 없을 때까지
    반복한다(최대 30회).
    """
    keys_of: dict[str, Counter[tuple[str, str, str]]] = {}
    for sid, ds in bodies_of.items():
        c: Counter[tuple[str, str, str]] = Counter()
        for d in ds:
            c[("genre", d.artifact, d.genre)] += 1
            c[("len", d.artifact, lengths[d.body_id])] += 1
        keys_of[sid] = c
    counts: dict[str, Counter[tuple[str, str, str]]] = {f: Counter() for f in fams}
    totals: Counter[tuple[str, str, str]] = Counter()
    for sid, f in fam_of_scn.items():
        counts[f].update(keys_of[sid])
        totals.update(keys_of[sid])
    mean = {k: v / len(fams) for k, v in totals.items()}

    def delta(a: str, b: str) -> float:
        fa, fb = fam_of_scn[a], fam_of_scn[b]
        total = 0.0
        for k in set(keys_of[a]) | set(keys_of[b]):
            move = keys_of[b][k] - keys_of[a][k]  # fa는 b의 몫을 받고 a의 몫을 내놓는다
            if move == 0:
                continue
            for f, sign in ((fa, 1), (fb, -1)):
                old = counts[f][k]
                new = old + sign * move
                total += (new - mean[k]) ** 2 - (old - mean[k]) ** 2
        return total

    for _ in range(30):
        improved = False
        for type_ in sorted(by_type):
            ids = sorted(s.scenario_id for s in by_type[type_])
            for i, a in enumerate(ids):
                for b_ in ids[i + 1 :]:
                    if fam_of_scn[a] == fam_of_scn[b_] or delta(a, b_) >= -1e-9:
                        continue
                    fa, fb = fam_of_scn[a], fam_of_scn[b_]
                    counts[fa].subtract(keys_of[a])
                    counts[fa].update(keys_of[b_])
                    counts[fb].subtract(keys_of[b_])
                    counts[fb].update(keys_of[a])
                    fam_of_scn[a], fam_of_scn[b_] = fb, fa
                    improved = True
        if not improved:
            break


def _assign_families(
    b: Builder, drafts: list[BodyDraft], lengths: dict[str, str]
) -> tuple[dict[str, Family], dict[str, Family]]:
    """(시나리오 ID -> 계열, body_id -> 계열).

    시나리오는 유형별로 고르게 나누고 배경은 칸별로 맞춘다.
    """
    fams: list[Family] = list(b.wm.generation.families)
    rng = b.rng("families")
    by_type: dict[str, list[Scn]] = defaultdict(list)
    for s in b.scenarios:
        by_type[s.type].append(s)
    fam_of_scn: dict[str, Family] = {}
    bodies_of: dict[str, list[BodyDraft]] = defaultdict(list)
    for d in drafts:
        if d.scenario_id is not None:
            bodies_of[d.scenario_id].append(d)
    genre_cnt: dict[str, Counter[tuple[str, str]]] = {f: Counter() for f in fams}
    len_cnt: dict[str, Counter[tuple[str, str]]] = {f: Counter() for f in fams}
    total_cnt: Counter[str] = Counter()
    for type_ in sorted(by_type):
        group = sorted(by_type[type_], key=lambda s: s.scenario_id)
        rng.shuffle(group)
        in_type: Counter[str] = Counter()
        for s in group:
            mine = bodies_of[s.scenario_id]

            def cost(f: str, mine: list[BodyDraft] = mine) -> float:
                c = sum(
                    genre_cnt[f][(d.artifact, d.genre)]
                    + len_cnt[f][(d.artifact, lengths[d.body_id])]
                    for d in mine
                )
                return c + 0.01 * total_cnt[f]

            low_type = min(in_type[f] for f in fams)
            cands = [f for f in fams if in_type[f] == low_type]
            best = min(cost(f) for f in cands)
            tied = [f for f in cands if cost(f) == best]
            pick = tied[rng.randrange(len(tied))]
            fam_of_scn[s.scenario_id] = pick
            in_type[pick] += 1
            for d in mine:
                genre_cnt[pick][(d.artifact, d.genre)] += 1
                len_cnt[pick][(d.artifact, lengths[d.body_id])] += 1
                total_cnt[pick] += 1
    _balance_by_swaps(fams, by_type, bodies_of, lengths, fam_of_scn)
    genre_cnt = {f: Counter() for f in fams}
    len_cnt = {f: Counter() for f in fams}
    total_cnt = Counter()
    for sid, f in fam_of_scn.items():
        for d in bodies_of[sid]:
            genre_cnt[f][(d.artifact, d.genre)] += 1
            len_cnt[f][(d.artifact, lengths[d.body_id])] += 1
            total_cnt[f] += 1
    fam_of_body: dict[str, Family] = {}
    cell: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    for d in drafts:
        if d.scenario_id is not None:
            f = fam_of_scn[d.scenario_id]
            fam_of_body[d.body_id] = f
            cell[(d.artifact, d.genre, lengths[d.body_id])][f] += 1
    rest = sorted((d for d in drafts if d.scenario_id is None), key=lambda d: d.body_id)
    rng.shuffle(rest)
    for d in rest:
        key = (d.artifact, d.genre, lengths[d.body_id])

        def prio(f: str, d: BodyDraft = d, key: tuple[str, str, str] = key) -> tuple[int, ...]:
            return (
                cell[key][f],
                genre_cnt[f][(d.artifact, d.genre)],
                len_cnt[f][(d.artifact, lengths[d.body_id])],
                total_cnt[f],
            )

        low = min(prio(f) for f in fams)
        cands2 = [f for f in fams if prio(f) == low]
        pick2 = cands2[rng.randrange(len(cands2))]
        fam_of_body[d.body_id] = pick2
        cell[key][pick2] += 1
        genre_cnt[pick2][(d.artifact, d.genre)] += 1
        len_cnt[pick2][(d.artifact, lengths[d.body_id])] += 1
        total_cnt[pick2] += 1
    return fam_of_scn, fam_of_body


def _finalize(b: Builder, seed: int, world_sha: str, sha: str) -> Skeleton:
    drafts = [b.bodies[k] for k in sorted(b.bodies)]
    lengths = _assign_lengths(b, drafts)
    fam_of_scn, fam_of_body = _assign_families(b, drafts, lengths)
    bodies: dict[str, BodySpec] = {}
    for d in drafts:
        cls = lengths[d.body_id]
        bodies[d.body_id] = BodySpec(
            artifact=d.artifact,
            genre=d.genre,
            length_class=cls,
            target_chars=TARGET_CHARS[cls],
            context=d.context,
            required_facts=d.required,
            forbidden_facts=d.forbidden,
            template_text=d.template,
            generator_family=fam_of_body[d.body_id],
        )
    accounts: dict[str, SkAccount] = {}
    for u in b.wm.users:
        a = b.acc[u.account_id]
        events = sorted(a.events, key=lambda e: (e.start, e.id))
        msgs = sorted(a.messages, key=lambda m: (m.sent_at, m.id))
        order = {m.id: i for i, m in enumerate(msgs)}
        threads = [
            SkThread(thread_id=t, message_ids=sorted(ids, key=lambda i: order[i]))
            for t, ids in sorted(a.thread_msgs.items())
        ]
        accounts[u.account_id] = SkAccount(
            email=u.email,
            name=u.name,
            time_zone=u.time_zone,
            purpose=u.purpose,
            calendars=a.calendars,
            events=events,
            threads=threads,
            messages=msgs,
            files=sorted(a.files, key=lambda f: f.id),
        )
    people = {
        p.key: SkPerson(
            key=p.key,
            name=p.name,
            email=p.email,
            company=p.company,
            role=p.role,
            relation=p.relation,
            works_with=p.works_with,
        )
        for p in b.wm.people
    }
    records = [
        ScenarioRecord(
            scenario_id=s.scenario_id,
            type=s.type,
            account_id=s.account_id,
            as_of=s.as_of,
            generator_family=fam_of_scn[s.scenario_id],
            evidence=s.evidence,
            distractors=s.distractors,
            truth=s.truth,
        )
        for s in sorted(b.scenarios, key=lambda s: s.scenario_id)
    ]
    mailboxes: dict[str, SearchMailbox] = {}
    for i, acct in enumerate(b.search_accts):
        mailboxes[acct] = SearchMailbox(
            size=len(b.acc[acct].messages),
            subset_of=b.search_accts[i + 1] if i + 1 < len(b.search_accts) else None,
            message_by_body=dict(sorted(b.search_msg_by_body[acct].items())),
            file_by_body=dict(sorted(b.search_file_by_body[acct].items())),
        )
    return Skeleton(
        schema_version=SCHEMA_VERSION,
        seed=seed,
        world_model_sha256=world_sha,
        generator_git_sha=sha,
        snapshot_time=b.snapshot,
        accounts=accounts,
        people=people,
        bodies=bodies,
        scenarios=records,
        search_mailboxes=mailboxes,
        notes=NOTES,
    )


def build_skeleton(wm: WorldModel, seed: int, *, world_sha256: str, git_sha_: str) -> Skeleton:
    """세계 모델과 시드로 뼈대를 만들고 검증한다. 검증 실패는 `SkeletonError`다."""
    from orbit.evaldata.v1.verify import verify_skeleton

    b = Builder(wm, seed)
    run_scenarios(b)
    fill_background(b)
    skeleton = _finalize(b, seed, world_sha256, git_sha_)
    verify_skeleton(wm, skeleton)
    return skeleton


# ---------------------------------------------------------------- 산출물


def dump_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def skeleton_json(skeleton: Skeleton) -> str:
    return dump_json(skeleton.model_dump(mode="json", by_alias=True))


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def generate(
    world_path: str | Path,
    seed: int,
    out: str | Path,
    sha: str | None = None,
    *,
    template_world: bool = False,
) -> Skeleton:
    from orbit.evaldata.v1.export import export_world
    from orbit.evaldata.v1.stats import skeleton_stats

    wm = load_world_model(world_path)
    skeleton = build_skeleton(
        wm,
        seed,
        world_sha256=file_sha256(world_path),
        git_sha_=sha if sha is not None else git_sha(Path(__file__).parent),
    )
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "skeleton.json").write_text(skeleton_json(skeleton), encoding="utf-8")
    (out_dir / "skeleton-stats.json").write_text(
        dump_json(skeleton_stats(wm, skeleton)), encoding="utf-8"
    )
    if template_world:
        (out_dir / "world.template.json").write_text(
            dump_json(export_world(skeleton)), encoding="utf-8"
        )
    return skeleton


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Orbit Sim 세계 v1 구조 생성 (LLM 없음)")
    parser.add_argument("--world", required=True, help="세계 모델 TOML")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", required=True, help="출력 디렉터리(저장소 밖 임시 위치 권장)")
    parser.add_argument("--git-sha", default=None)
    parser.add_argument(
        "--template-world",
        action="store_true",
        help="템플릿 본문으로 채운 world.template.json도 쓴다",
    )
    args = parser.parse_args(argv)
    t0 = time.monotonic()
    sk = generate(args.world, args.seed, args.out, args.git_sha, template_world=args.template_world)
    n_mail = sum(len(a.messages) for a in sk.accounts.values())
    print(
        f"생성 완료: 계정 {len(sk.accounts)}, 메일 {n_mail}, 본문 명세 {len(sk.bodies)}, "
        f"시나리오 {len(sk.scenarios)} -> {args.out} ({time.monotonic() - t0:.1f}초)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
