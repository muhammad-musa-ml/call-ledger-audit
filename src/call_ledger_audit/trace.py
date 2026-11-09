"""One member's whole history in time order: attempts, write-backs, suppression entries, findings."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from .checks import Finding
from .model import InputError, Ledger


def member_timeline(ledger: Ledger, member_id: str, findings: list[Finding]) -> list[str]:
    """Lines for one member. Raises InputError when the member has no attempt in the log."""
    attempts = sorted(
        (a for a in ledger.attempts if a.member_id == member_id), key=lambda a: (a.started_at, a.attempt_id)
    )
    if not attempts:
        raise InputError(f"no attempts for member {member_id!r}")
    zone = ZoneInfo(attempts[0].tz)
    phones = {a.phone for a in attempts}
    mine = {a.attempt_id for a in attempts}
    by_attempt: dict[str, list[Finding]] = {}
    for finding in findings:
        by_attempt.setdefault(finding.attempt_id, []).append(finding)

    events = []
    for a in attempts:
        zone_a = ZoneInfo(a.tz)
        disclosure = "disclosure played" if a.disclosure_played else "no disclosure"
        events.append((a.started_at, 0, a.attempt_id, a.started_at.astimezone(zone_a), f"call {a.attempt_id}  {a.disposition}, {disclosure}"))
    for w in ledger.writebacks:
        if w.attempt_id in mine:
            extra = ""
            if w.outcome == "promise_to_pay":
                extra = f" amount={w.promise_amount} date={w.promise_date}"
            events.append((w.written_at, 1, w.attempt_id, w.written_at.astimezone(zone), f"write-back for {w.attempt_id}  {w.outcome}{extra}"))
    for s in ledger.suppressions:
        if s.member_id == member_id or (s.phone is not None and s.phone in phones):
            events.append((s.added_at, 1, "", s.added_at.astimezone(zone), f"suppression entry  {s.source}"))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    lines = [f"member {member_id}, phone {', '.join(sorted(phones))}"]
    for instant, kind, attempt_id, local, text in events:
        lines.append(f"{local:%Y-%m-%d %H:%M:%S %Z}  {instant:%H:%M:%SZ}  {text}")
        if kind == 0:
            for finding in sorted(by_attempt.get(attempt_id, []), key=lambda f: f.rule):
                lines.append(f"    ! {finding.rule}: {finding.detail}")
    return lines
