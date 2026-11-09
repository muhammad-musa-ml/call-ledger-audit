"""The nine checks. Each takes a Ledger and a Policy and returns findings.

A check reads only the ledger and the policy. It never guesses what a client's
rules should be: the limits it applies come from the policy. Two things are
fixed in the code: the 7-day window is 7 x 24 hours, and the table of outcomes
each disposition can produce is ALLOWED_OUTCOMES.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from zoneinfo import ZoneInfo

from .model import Attempt, Ledger, Policy


@dataclass(frozen=True)
class Finding:
    rule: str
    attempt_id: str
    member_id: str | None
    detail: str


def _local(attempt: Attempt):
    return attempt.started_at.astimezone(ZoneInfo(attempt.tz))


def _hhmm(value) -> str:
    return value.strftime("%H:%M")


def outside_window(ledger: Ledger, policy: Policy) -> list[Finding]:
    """The attempt began outside the calling window in the member's own time zone.

    The window is [start, end): a call at exactly the end time is outside.
    """
    found = []
    for attempt in ledger.attempts:
        local = _local(attempt)
        if not (policy.window_start <= local.time() < policy.window_end):
            found.append(
                Finding(
                    "outside_window",
                    attempt.attempt_id,
                    attempt.member_id,
                    f"began {local:%Y-%m-%d %H:%M:%S} {attempt.tz}; window is "
                    f"{_hhmm(policy.window_start)} to {_hhmm(policy.window_end)}",
                )
            )
    return found


def _suppression_index(ledger: Ledger):
    by_member = defaultdict(list)
    by_phone = defaultdict(list)
    for entry in ledger.suppressions:
        if entry.member_id is not None:
            by_member[entry.member_id].append(entry)
        if entry.phone is not None:
            by_phone[entry.phone].append(entry)
    return by_member, by_phone


def suppressed(ledger: Ledger, policy: Policy) -> list[Finding]:
    """The attempt began at or after a suppression entry for the member or the phone.

    An entry that names the attempt as the call that produced it is not held against
    that call: a platform may stamp an opt-out with the call's own start time.
    """
    by_member, by_phone = _suppression_index(ledger)
    found = []
    for attempt in ledger.attempts:
        entries = by_member.get(attempt.member_id, []) + by_phone.get(attempt.phone, [])
        active = [e for e in entries if e.added_at <= attempt.started_at and e.attempt_id != attempt.attempt_id]
        if active:
            first = min(active, key=lambda e: (e.added_at, e.source))
            found.append(
                Finding(
                    "suppressed",
                    attempt.attempt_id,
                    attempt.member_id,
                    f"{first.source} entry added {first.added_at:%Y-%m-%dT%H:%M:%SZ}, "
                    f"attempt began {attempt.started_at:%Y-%m-%dT%H:%M:%SZ}",
                )
            )
    return found


def no_disclosure(ledger: Ledger, policy: Policy) -> list[Finding]:
    """A conversation took place and the disclosure was not played."""
    if not policy.require_disclosure:
        return []
    return [
        Finding("no_disclosure", a.attempt_id, a.member_id, f"{a.disposition} call with disclosure_played false")
        for a in ledger.attempts
        if a.disposition in ("connected", "opt_out") and not a.disclosure_played
    ]


def attempt_cap(ledger: Ledger, policy: Policy) -> list[Finding]:
    """More attempts to one member than the policy allows, per local day or per rolling 7 days.

    Every attempt past the cap is a finding, not only the first. The 7-day window
    is (t - 7 days, t]: an attempt exactly 7 days earlier does not count.
    """
    by_member = defaultdict(list)
    for attempt in ledger.attempts:
        by_member[attempt.member_id].append(attempt)
    found = []
    for member_id, attempts in by_member.items():
        attempts.sort(key=lambda a: (a.started_at, a.attempt_id))
        per_day = defaultdict(int)
        for index, attempt in enumerate(attempts):
            reasons = []
            day = _local(attempt).date()
            per_day[day] += 1
            if per_day[day] > policy.max_attempts_per_day:
                reasons.append(f"{per_day[day]} attempts on {day} (cap {policy.max_attempts_per_day})")
            since = attempt.started_at - timedelta(days=7)
            in_week = sum(1 for other in attempts[: index + 1] if other.started_at > since)
            if in_week > policy.max_attempts_per_7_days:
                reasons.append(f"{in_week} attempts in the 7 days to this one (cap {policy.max_attempts_per_7_days})")
            if reasons:
                found.append(Finding("attempt_cap", attempt.attempt_id, member_id, "; ".join(reasons)))
    return found


def _writebacks_by_attempt(ledger: Ledger):
    index = defaultdict(list)
    for writeback in ledger.writebacks:
        index[writeback.attempt_id].append(writeback)
    return index


def writeback_deadline(ledger: Ledger, policy: Policy) -> list[Finding]:
    """A write-back is stamped before its call began, or an attempt that needs one has none by the deadline.

    The deadline runs from the attempt's started_at; there is no end-of-call field. Which
    dispositions need a write-back is policy.writeback_dispositions. A write-back at exactly
    the deadline is on time. A write-back stamped before the call is reported for any disposition.
    """
    index = _writebacks_by_attempt(ledger)
    found = []
    for attempt in ledger.attempts:
        written = index.get(attempt.attempt_id, [])
        first = min((w.written_at for w in written), default=None)
        if first is not None and first < attempt.started_at:
            found.append(
                Finding(
                    "writeback_deadline",
                    attempt.attempt_id,
                    attempt.member_id,
                    f"first write-back {first:%Y-%m-%dT%H:%M:%SZ} is before the call began "
                    f"{attempt.started_at:%Y-%m-%dT%H:%M:%SZ}",
                )
            )
            continue
        if attempt.disposition not in policy.writeback_dispositions:
            continue
        deadline = attempt.started_at + timedelta(minutes=policy.writeback_deadline_minutes)
        if first is None:
            found.append(Finding("writeback_deadline", attempt.attempt_id, attempt.member_id, "no write-back"))
        elif first > deadline:
            found.append(
                Finding(
                    "writeback_deadline",
                    attempt.attempt_id,
                    attempt.member_id,
                    f"first write-back {first:%Y-%m-%dT%H:%M:%SZ}, deadline {deadline:%Y-%m-%dT%H:%M:%SZ}",
                )
            )
    return found


def orphan_writeback(ledger: Ledger, policy: Policy) -> list[Finding]:
    """A write-back names an attempt that is not in the call log."""
    known = {a.attempt_id for a in ledger.attempts}
    return [
        Finding("orphan_writeback", w.attempt_id, None, f"write-back {w.outcome} at {w.written_at:%Y-%m-%dT%H:%M:%SZ}")
        for w in ledger.writebacks
        if w.attempt_id not in known
    ]


ALLOWED_OUTCOMES = {
    "connected": {"promise_to_pay", "callback_requested", "declined"},
    "opt_out": {"opt_out"},
    "no_answer": {"no_contact"},
    "voicemail": {"no_contact"},
}


def outcome_mismatch(ledger: Ledger, policy: Policy) -> list[Finding]:
    """A write-back outcome the call's disposition cannot produce, or two that disagree."""
    index = _writebacks_by_attempt(ledger)
    found = []
    for attempt in ledger.attempts:
        written = index.get(attempt.attempt_id, [])
        reasons = []
        for w in sorted(written, key=lambda w: (w.written_at, w.outcome)):
            if w.outcome not in ALLOWED_OUTCOMES[attempt.disposition]:
                reasons.append(f"{attempt.disposition} call written back as {w.outcome}")
        if len({w.outcome for w in written}) > 1:
            reasons.append("write-backs disagree: " + ", ".join(sorted({w.outcome for w in written})))
        if reasons:
            found.append(Finding("outcome_mismatch", attempt.attempt_id, attempt.member_id, "; ".join(reasons)))
    return found


def promise_incomplete(ledger: Ledger, policy: Policy) -> list[Finding]:
    """A promise to pay with no amount, a zero or negative amount, or no date."""
    member_of = {a.attempt_id: a.member_id for a in ledger.attempts}
    found = []
    for w in ledger.writebacks:
        if w.outcome != "promise_to_pay":
            continue
        missing = []
        if w.promise_amount is None:
            missing.append("no amount")
        elif w.promise_amount <= 0:
            missing.append(f"amount {w.promise_amount}")
        if w.promise_date is None:
            missing.append("no date")
        if missing:
            found.append(Finding("promise_incomplete", w.attempt_id, member_of.get(w.attempt_id), ", ".join(missing)))
    return found


def opt_out_not_suppressed(ledger: Ledger, policy: Policy) -> list[Finding]:
    """A member opted out, on the call or in a write-back, and no suppression entry exists by the deadline.

    An entry added before the call also counts: the member was already suppressed.
    An entry exactly at the deadline is on time.
    """
    by_member, by_phone = _suppression_index(ledger)
    index = _writebacks_by_attempt(ledger)
    found = []
    for attempt in ledger.attempts:
        on_call = attempt.disposition == "opt_out"
        if not on_call and not any(w.outcome == "opt_out" for w in index.get(attempt.attempt_id, [])):
            continue
        deadline = attempt.started_at + timedelta(minutes=policy.suppression_deadline_minutes)
        entries = by_member.get(attempt.member_id, []) + by_phone.get(attempt.phone, [])
        if not any(e.added_at <= deadline for e in entries):
            where = "on the call" if on_call else "in the write-back"
            found.append(
                Finding(
                    "opt_out_not_suppressed",
                    attempt.attempt_id,
                    attempt.member_id,
                    f"opt-out {where}, no suppression entry by {deadline:%Y-%m-%dT%H:%M:%SZ}",
                )
            )
    return found


RULES = {
    "outside_window": outside_window,
    "suppressed": suppressed,
    "no_disclosure": no_disclosure,
    "attempt_cap": attempt_cap,
    "writeback_deadline": writeback_deadline,
    "orphan_writeback": orphan_writeback,
    "outcome_mismatch": outcome_mismatch,
    "promise_incomplete": promise_incomplete,
    "opt_out_not_suppressed": opt_out_not_suppressed,
}


def run_checks(ledger: Ledger, policy: Policy) -> list[Finding]:
    """Every check, findings ordered by attempt id, rule and detail, so row order never shows."""
    findings = [f for check in RULES.values() for f in check(ledger, policy)]
    return sorted(findings, key=lambda f: (f.attempt_id, f.rule, f.detail))
