"""Seeded synthetic exports with known answers.

Every dataset has two parts. The clean part is ordinary traffic plus calls that
sit on a boundary and must NOT be flagged: the first and last second of the
window, the daylight-saving days, a write-back exactly at its deadline, the
last attempt the cap allows. The planted part adds self-contained defects
(each on its own member, built to trip exactly one rule) and records which
attempt each one should be reported on.

Everything is synthetic. The same seed gives the same bytes.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .model import Attempt, Ledger, Policy, Suppression, Writeback

ZONES = ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Phoenix")
# Each fortnight holds one US clock change: 2026-03-08 and 2026-11-01.
FORTNIGHTS = (date(2026, 3, 2), date(2026, 10, 26))
# Phone numbers are +1000555 plus a four-digit counter, so a dataset holds under 10000 members.
# 4000 ordinary members, 20 repetitions of 25 variants and the boundary cases fit under that.
MAX_MEMBERS = 4000
MAX_REPS = 20

PLANTED = {
    "outside_window": ("before_open_boundary", "at_close_boundary", "early_morning", "late_night", "other_zone_looks_inside"),
    "suppressed": ("dnc_same_second", "dnc_minutes_before", "shared_phone_entry", "after_opt_out_call"),
    "no_disclosure": ("connected", "opt_out"),
    "attempt_cap": ("third_call_in_a_day", "sixth_call_in_a_week"),
    "writeback_deadline": ("never_written", "one_second_late"),
    "orphan_writeback": ("unknown_attempt",),
    "outcome_mismatch": ("voicemail_promise", "opt_out_declined", "connected_no_contact", "two_writebacks_disagree"),
    "promise_incomplete": ("no_amount", "zero_amount", "no_date"),
    "opt_out_not_suppressed": ("no_entry", "one_second_late"),
}


@dataclass
class Dataset:
    ledger: Ledger
    policy: Policy
    planted: list  # (rule, attempt_id, variant), sorted


def local_to_utc(day: date, clock: time, tz: str) -> datetime:
    return datetime.combine(day, clock, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


def _clock(seconds: int) -> time:
    return time(seconds // 3600, seconds % 3600 // 60, seconds % 60)


class _Build:
    def __init__(self, rng: random.Random, policy: Policy):
        self.rng = rng
        self.policy = policy
        self.attempts: list[Attempt] = []
        self.writebacks: list[Writeback] = []
        self.suppressions: list[Suppression] = []
        self.planted: list = []
        self._attempts = 0
        self._members = 0
        self._orphans = 0

    def member(self) -> tuple[str, str]:
        self._members += 1
        return f"m{self._members:04d}", f"+1000555{self._members:04d}"  # area code 000 does not exist

    def attempt(self, member, phone, tz, instant, disposition, disclosure=None) -> Attempt:
        self._attempts += 1
        if disclosure is None:
            disclosure = disposition in ("connected", "opt_out")
        made = Attempt(f"a{self._attempts:05d}", member, phone, tz, instant, disposition, disclosure)
        self.attempts.append(made)
        return made

    def writeback(self, attempt, seconds, outcome, amount=None, promise_date=None) -> None:
        when = attempt.started_at + timedelta(seconds=seconds)
        self.writebacks.append(Writeback(attempt.attempt_id, when, outcome, amount, promise_date))

    def suppress(self, member, phone, instant, source, attempt_id=None) -> None:
        self.suppressions.append(Suppression(member, phone, instant, source, attempt_id))

    def call(self, member, phone, tz, instant, disposition="connected", *, wb=300, outcome=None,
             amount=None, promise_date=None, suppress_after=600, disclosure=None) -> Attempt:
        """An attempt and the write-back and suppression entry a correct run would leave.

        wb is the write-back delay in seconds, None for no write-back. suppress_after is the
        delay of the opt-out suppression entry, None for none. outcome overrides the default.
        """
        made = self.attempt(member, phone, tz, instant, disposition, disclosure)
        if disposition == "connected" and wb is not None:
            self.writeback(made, wb, outcome or "declined", amount, promise_date)
        elif disposition == "opt_out":
            if wb is not None:
                self.writeback(made, wb, outcome or "opt_out", amount, promise_date)
            if suppress_after is not None:
                self.suppress(member, phone, instant + timedelta(seconds=suppress_after), "opt_out_call", made.attempt_id)
        elif disposition in ("no_answer", "voicemail") and outcome is not None and wb is not None:
            self.writeback(made, wb, outcome, amount, promise_date)
        return made


def _local_date(attempt: Attempt) -> date:
    return attempt.started_at.astimezone(ZoneInfo(attempt.tz)).date()


def _in_window_clock(rng: random.Random, policy: Policy) -> time:
    low = policy.window_start.hour * 3600 + policy.window_start.minute * 60
    high = policy.window_end.hour * 3600 + policy.window_end.minute * 60
    return _clock(rng.randint(low, high - 1))


def _clean_member(b: _Build) -> None:
    rng = b.rng
    member, phone = b.member()
    tz = rng.choice(ZONES)
    fortnight = rng.choice(FORTNIGHTS)
    last = None
    for offset in sorted(rng.sample((0, 3, 6, 9, 12), rng.choice((1, 2, 3)))):
        instant = local_to_utc(fortnight + timedelta(days=offset), _in_window_clock(rng, b.policy), tz)
        disposition = rng.choices(("no_answer", "voicemail", "connected", "opt_out"), weights=(40, 20, 33, 7))[0]
        disclosure = rng.random() < 0.5 if disposition == "voicemail" else None
        last = b.attempt(member, phone, tz, instant, disposition, disclosure)
        deadline = b.policy.writeback_deadline_minutes * 60
        if disposition == "connected":
            outcome = rng.choices(("promise_to_pay", "callback_requested", "declined"), weights=(45, 25, 30))[0]
            if outcome == "promise_to_pay":
                promised = _local_date(last) + timedelta(days=rng.randint(3, 14))
                b.writeback(last, rng.randint(60, deadline), outcome, round(rng.uniform(25, 900), 2), promised)
            else:
                b.writeback(last, rng.randint(60, deadline), outcome)
        elif disposition == "opt_out":
            b.writeback(last, rng.randint(60, deadline), "opt_out")
            wait = rng.randint(0, b.policy.suppression_deadline_minutes * 60)
            b.suppress(member, phone, instant + timedelta(seconds=wait), "opt_out_call", last.attempt_id)
            break
        elif rng.random() < 0.7:
            b.writeback(last, rng.randint(60, deadline), "no_contact")
    if last.disposition != "opt_out" and rng.random() < 0.1:
        b.suppress(member, phone, last.started_at + timedelta(seconds=rng.randint(3600, 5 * 86400)), "dnc")


def _clean_boundaries(b: _Build) -> None:
    """Calls a correct tool must leave alone, each one on a boundary of some rule."""
    deadline = b.policy.writeback_deadline_minutes * 60
    base = date(2026, 3, 3)
    chicago, la, denver, phoenix, new_york = (
        "America/Chicago", "America/Los_Angeles", "America/Denver", "America/Phoenix", "America/New_York",
    )

    def solo(tz):
        member, phone = b.member()
        return member, phone, tz

    # The first and the last second of the window.
    b.call(*solo(chicago), local_to_utc(base, time(8, 0, 0), chicago))
    b.call(*solo(la), local_to_utc(base, time(20, 59, 59), la))
    # Clock changes: the offset differs from the one a fixed UTC offset would use.
    member, phone, tz = solo(chicago)
    for clock in (time(8, 30), time(20, 30)):
        b.call(member, phone, tz, local_to_utc(date(2026, 3, 8), clock, tz), "no_answer")
    member, phone, tz = solo(chicago)
    for clock in (time(8, 15), time(20, 45)):
        b.call(member, phone, tz, local_to_utc(date(2026, 11, 1), clock, tz), "no_answer")
    b.call(*solo(new_york), local_to_utc(date(2026, 11, 1), time(8, 0, 0), new_york))
    # Phoenix has no daylight saving: 20:30 there in July would be 21:30, outside, in a zone that has it.
    b.call(*solo(phoenix), local_to_utc(date(2026, 7, 15), time(20, 30, 0), phoenix))
    # Two attempts in one local day: the daily cap exactly.
    member, phone, tz = solo(denver)
    for clock in (time(9, 0), time(15, 0)):
        b.call(member, phone, tz, local_to_utc(base, clock, tz), "no_answer")
    # Five attempts in three days, then a sixth exactly seven days after the first.
    member, phone, tz = solo(denver)
    local = [(base, time(9, 0)), (base, time(13, 0)), (base + timedelta(days=1), time(9, 0)),
             (base + timedelta(days=1), time(13, 0)), (base + timedelta(days=2), time(9, 0))]
    firsts = [b.call(member, phone, tz, local_to_utc(day, clock, tz), "no_answer") for day, clock in local]
    b.call(member, phone, tz, firsts[0].started_at + timedelta(days=7), "no_answer")
    # A write-back exactly at its deadline, and an opt-out entry exactly at its deadline.
    b.call(*solo(chicago), local_to_utc(base, time(10, 0, 0), chicago), wb=deadline)
    b.call(*solo(chicago), local_to_utc(base, time(10, 0, 0), chicago), "opt_out",
           suppress_after=b.policy.suppression_deadline_minutes * 60)
    # An opt-out entry stamped at the call's own start, naming the call, is not held against it.
    b.call(*solo(chicago), local_to_utc(base, time(10, 30, 0), chicago), "opt_out", suppress_after=0)
    # A do-not-call entry one second after the call does not reach back to it.
    member, phone, tz = solo(chicago)
    call = b.call(member, phone, tz, local_to_utc(base, time(11, 0, 0), tz))
    b.suppress(member, phone, call.started_at + timedelta(seconds=1), "dnc")
    # A phone shared by two members: an entry added later for the other member's call.
    first, shared, tz = solo(chicago)
    second, _, _ = solo(chicago)
    b.call(second, shared, tz, local_to_utc(base - timedelta(days=1), time(10, 0, 0), tz))
    call = b.call(first, shared, tz, local_to_utc(base, time(10, 0, 0), tz))
    b.suppress(second, shared, call.started_at + timedelta(hours=1), "dnc")
    # Calls that need no write-back and no disclosure.
    b.call(*solo(chicago), local_to_utc(base, time(12, 0, 0), chicago), "no_answer")
    b.call(*solo(chicago), local_to_utc(base, time(12, 0, 0), chicago), "voicemail", disclosure=False)
    # The smallest positive promise.
    b.call(*solo(chicago), local_to_utc(base, time(13, 0, 0), chicago), outcome="promise_to_pay",
           amount=0.01, promise_date=base + timedelta(days=5))


def _plant(b: _Build, rule: str, variant: str) -> None:
    rng = b.rng
    deadline = b.policy.writeback_deadline_minutes * 60
    member, phone = b.member()
    tz = rng.choice(ZONES)
    day = rng.choice(FORTNIGHTS) + timedelta(days=rng.randint(0, 13))
    during = local_to_utc(day, _clock(rng.randint(9 * 3600, 17 * 3600)), tz)

    def mark(made):
        b.planted.append((rule, made.attempt_id, variant))

    if rule == "outside_window":
        if variant == "other_zone_looks_inside":
            tz = "America/Los_Angeles"
            clock = _clock(rng.randint(6 * 3600, 7 * 3600 + 1800))
        else:
            clock = {
                "before_open_boundary": time(7, 59, 59),
                "at_close_boundary": time(21, 0, 0),
                "early_morning": _clock(rng.randint(3 * 3600, 7 * 3600 + 1800)),
                "late_night": _clock(rng.randint(21 * 3600 + 1800, 23 * 3600 + 1800)),
            }[variant]
        mark(b.call(member, phone, tz, local_to_utc(day, clock, tz)))
    elif rule == "suppressed":
        if variant == "dnc_same_second":
            b.suppress(member, phone, during, "dnc")
        elif variant == "dnc_minutes_before":
            b.suppress(member, phone, during - timedelta(minutes=rng.randint(1, 600)), "dnc")
        elif variant == "shared_phone_entry":
            other, _ = b.member()
            b.suppress(other, phone, during - timedelta(days=1), "dnc")
        else:
            b.call(member, phone, tz, during - timedelta(days=2), "opt_out")
        mark(b.call(member, phone, tz, during))
    elif rule == "no_disclosure":
        mark(b.call(member, phone, tz, during, variant, disclosure=False))
    elif rule == "attempt_cap":
        if variant == "third_call_in_a_day":
            plan = [(0, 9), (0, 12), (0, 15)]
        else:
            plan = [(0, 9), (0, 13), (1, 9), (1, 13), (2, 9), (2, 13)]
        calls = [
            b.call(member, phone, tz, local_to_utc(day + timedelta(days=d), time(h, rng.randint(0, 59)), tz), "no_answer")
            for d, h in plan
        ]
        mark(calls[-1])
    elif rule == "writeback_deadline":
        mark(b.call(member, phone, tz, during, wb=None if variant == "never_written" else deadline + 1))
    elif rule == "orphan_writeback":
        b._orphans += 1
        orphan = f"x{b._orphans:05d}"
        b.writebacks.append(Writeback(orphan, during, "no_contact", None, None))
        b.planted.append((rule, orphan, variant))
    elif rule == "outcome_mismatch":
        promised = day + timedelta(days=7)
        if variant == "voicemail_promise":
            mark(b.call(member, phone, tz, during, "voicemail", outcome="promise_to_pay", amount=80.0, promise_date=promised))
        elif variant == "opt_out_declined":
            mark(b.call(member, phone, tz, during, "opt_out", outcome="declined"))
        elif variant == "connected_no_contact":
            mark(b.call(member, phone, tz, during, outcome="no_contact"))
        else:
            made = b.call(member, phone, tz, during, outcome="callback_requested")
            b.writeback(made, 900, "declined")
            mark(made)
    elif rule == "promise_incomplete":
        amount = {"no_amount": None, "zero_amount": 0, "no_date": 100.0}[variant]
        promised = None if variant == "no_date" else day + timedelta(days=7)
        mark(b.call(member, phone, tz, during, outcome="promise_to_pay", amount=amount, promise_date=promised))
    elif rule == "opt_out_not_suppressed":
        late = b.policy.suppression_deadline_minutes * 60 + 1
        mark(b.call(member, phone, tz, during, "opt_out", suppress_after=None if variant == "no_entry" else late))
    else:
        raise ValueError(f"unknown rule {rule!r}")


def generate(seed: int, members: int = 150, reps: int = 3, plant: bool = True) -> Dataset:
    """One dataset. members is the number of ordinary members (0 leaves only the boundary cases);
    reps is how many times each defect variant is planted."""
    if not 0 <= members <= MAX_MEMBERS or not 1 <= reps <= MAX_REPS:
        raise ValueError(f"members must be 0 to {MAX_MEMBERS} and reps 1 to {MAX_REPS}")
    rng = random.Random(seed)
    policy = Policy()
    b = _Build(rng, policy)
    for _ in range(members):
        _clean_member(b)
    _clean_boundaries(b)
    if plant:
        for rule, variants in PLANTED.items():
            for variant in variants:
                for _ in range(reps):
                    _plant(b, rule, variant)
    rng.shuffle(b.attempts)
    rng.shuffle(b.writebacks)
    rng.shuffle(b.suppressions)
    return Dataset(Ledger(b.attempts, b.writebacks, b.suppressions), policy, sorted(b.planted))


def score(findings, planted) -> tuple[set, set]:
    """(missed, extra) as sets of (rule, attempt_id): planted and not reported, reported and not planted."""
    wanted = {(rule, attempt_id) for rule, attempt_id, _ in planted}
    reported = {(f.rule, f.attempt_id) for f in findings}
    return wanted - reported, reported - wanted


def _stamp(instant: datetime) -> str:
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


def _line(row: dict) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"


def write_dataset(dataset: Dataset, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    policy = dataset.policy
    files = {
        "attempts.jsonl": [
            {"attempt_id": a.attempt_id, "member_id": a.member_id, "phone": a.phone, "tz": a.tz,
             "started_at": _stamp(a.started_at), "disposition": a.disposition,
             "disclosure_played": a.disclosure_played}
            for a in dataset.ledger.attempts
        ],
        "writebacks.jsonl": [
            {"attempt_id": w.attempt_id, "written_at": _stamp(w.written_at), "outcome": w.outcome,
             "promise_amount": w.promise_amount,
             "promise_date": w.promise_date.isoformat() if w.promise_date else None}
            for w in dataset.ledger.writebacks
        ],
        "suppressions.jsonl": [
            {"member_id": s.member_id, "phone": s.phone, "added_at": _stamp(s.added_at), "source": s.source,
             "attempt_id": s.attempt_id}
            for s in dataset.ledger.suppressions
        ],
    }
    for name, rows in files.items():
        with (directory / name).open("w", encoding="utf-8", newline="\n") as handle:
            handle.writelines(_line(row) for row in rows)
    policy_row = {
        "window_start": policy.window_start.strftime("%H:%M"),
        "window_end": policy.window_end.strftime("%H:%M"),
        "max_attempts_per_day": policy.max_attempts_per_day,
        "max_attempts_per_7_days": policy.max_attempts_per_7_days,
        "writeback_deadline_minutes": policy.writeback_deadline_minutes,
        "suppression_deadline_minutes": policy.suppression_deadline_minutes,
        "require_disclosure": policy.require_disclosure,
        "writeback_dispositions": list(policy.writeback_dispositions),
    }
    (directory / "policy.json").write_text(json.dumps(policy_row, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    planted = [{"rule": r, "attempt_id": a, "variant": v} for r, a, v in dataset.planted]
    (directory / "planted.json").write_text(json.dumps(planted, indent=1) + "\n", encoding="utf-8", newline="\n")
