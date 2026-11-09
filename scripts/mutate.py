"""Break each rule on purpose and require the test suite to notice.

    python scripts/mutate.py

Every mutant is applied to its own fresh copy of the repository in a temporary folder; the repository
itself is never edited. A mutant is killed when pytest in the copy exits 1 (a test failed). Exit 0 means
the suite did not notice. Any other exit code, or an anchor that does not occur exactly once, is reported
as unrun and fails the script: an unrun mutant says nothing about the tests.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKS = "src/call_ledger_audit/checks.py"
MODEL = "src/call_ledger_audit/model.py"
TRACE = "src/call_ledger_audit/trace.py"
CLI = "src/call_ledger_audit/cli.py"
SUPPRESSED_HEAD = "entries = by_member.get(attempt.member_id, []) + by_phone.get(attempt.phone, [])\n        active ="

MUTANTS = [
    ("window end is inclusive", CHECKS, "if not (policy.window_start <= local.time() < policy.window_end):",
     "if not (policy.window_start <= local.time() <= policy.window_end):"),
    ("window start is exclusive", CHECKS, "if not (policy.window_start <= local.time() < policy.window_end):",
     "if not (policy.window_start < local.time() < policy.window_end):"),
    ("local time read in UTC", CHECKS, "return attempt.started_at.astimezone(ZoneInfo(attempt.tz))",
     "return attempt.started_at"),
    ("local time read at a fixed UTC-6", CHECKS, "return attempt.started_at.astimezone(ZoneInfo(attempt.tz))",
     'return attempt.started_at.astimezone(__import__("datetime").timezone(__import__("datetime").timedelta(hours=-6)))'),
    ("an entry at the call's start does not count", CHECKS,
     "e.added_at <= attempt.started_at and e.attempt_id != attempt.attempt_id]",
     "e.added_at < attempt.started_at and e.attempt_id != attempt.attempt_id]"),
    ("an entry naming the call is held against it", CHECKS,
     "e.added_at <= attempt.started_at and e.attempt_id != attempt.attempt_id]", "e.added_at <= attempt.started_at]"),
    ("suppression ignores the phone", CHECKS, SUPPRESSED_HEAD,
     "entries = by_member.get(attempt.member_id, [])\n        active ="),
    ("suppression ignores the member", CHECKS, SUPPRESSED_HEAD,
     "entries = by_phone.get(attempt.phone, [])\n        active ="),
    ("disclosure only checked when connected", CHECKS,
     'if a.disposition in ("connected", "opt_out") and not a.disclosure_played',
     'if a.disposition == "connected" and not a.disclosure_played'),
    ("disclosure policy ignored", CHECKS, "if not policy.require_disclosure:", "if False:"),
    ("daily cap off by one", CHECKS, "if per_day[day] > policy.max_attempts_per_day:",
     "if per_day[day] >= policy.max_attempts_per_day:"),
    ("weekly cap off by one", CHECKS, "if in_week > policy.max_attempts_per_7_days:",
     "if in_week >= policy.max_attempts_per_7_days:"),
    ("the attempt 7 days earlier counts", CHECKS, "if other.started_at > since", "if other.started_at >= since"),
    ("the week is 6 days", CHECKS, "since = attempt.started_at - timedelta(days=7)",
     "since = attempt.started_at - timedelta(days=6)"),
    ("cap day taken in UTC", CHECKS, "day = _local(attempt).date()", "day = attempt.started_at.date()"),
    ("write-back at the deadline is late", CHECKS, "if first > deadline:", "if first >= deadline:"),
    ("the latest write-back is the one judged", CHECKS, "first = min(w.written_at for w in written)",
     "first = max(w.written_at for w in written)"),
    ("write-back required only when connected", CHECKS,
     "if attempt.disposition not in policy.writeback_dispositions:", 'if attempt.disposition != "connected":'),
    ("write-back deadline fixed at 60 minutes", CHECKS,
     "deadline = attempt.started_at + timedelta(minutes=policy.writeback_deadline_minutes)",
     "deadline = attempt.started_at + timedelta(minutes=60)"),
    ("orphans inverted", CHECKS, "if w.attempt_id not in known", "if w.attempt_id in known"),
    ("outcomes never mismatched", CHECKS, "if w.outcome not in ALLOWED_OUTCOMES[attempt.disposition]:", "if False:"),
    ("disagreement needs three outcomes", CHECKS, "if len({w.outcome for w in written}) > 1:",
     "if len({w.outcome for w in written}) > 2:"),
    ("an opt-out call may be written back as declined", CHECKS, '"opt_out": {"opt_out"},',
     '"opt_out": {"opt_out", "declined"},'),
    ("a voicemail may carry a promise", CHECKS, '"voicemail": {"no_contact"},',
     '"voicemail": {"no_contact", "promise_to_pay"},'),
    ("a zero amount is accepted", CHECKS, "elif w.promise_amount <= 0:", "elif w.promise_amount < 0:"),
    ("a missing promise date is accepted", CHECKS, "if w.promise_date is None:", "if False:"),
    ("a late suppression entry is on time", CHECKS, "if not any(e.added_at <= deadline for e in entries):",
     "if not any(True for e in entries):"),
    ("an entry at the suppression deadline is late", CHECKS, "if not any(e.added_at <= deadline for e in entries):",
     "if not any(e.added_at < deadline for e in entries):"),
    ("suppression deadline fixed at 30 minutes", CHECKS,
     "deadline = attempt.started_at + timedelta(minutes=policy.suppression_deadline_minutes)",
     "deadline = attempt.started_at + timedelta(minutes=30)"),
    ("a timestamp with no offset is accepted", MODEL, "if parsed.tzinfo is None:", "if False:"),
    ("a duplicate attempt id is accepted", MODEL, "if attempt_id in seen:", "if False:"),
    ("an unknown policy key is accepted", MODEL, "if unknown:", "if False:"),
    ("trace is not in time order", TRACE, "events.sort(key=lambda e: (e[0], e[1], e[2]))", "pass"),
    ("trace shows no findings", TRACE, "for finding in sorted(by_attempt.get(attempt_id, []), key=lambda f: f.rule):",
     "for finding in []:"),
    ("check exits 0 on findings", CLI, "return 1 if findings else 0", "return 0"),
    ("bad input exits 1", CLI, 'print(f"input error: {error}", file=sys.stderr)\n        return 2',
     'print(f"input error: {error}", file=sys.stderr)\n        return 1'),
]


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def run_pytest(copy: Path) -> int:
    env = {**os.environ, "PYTHONPATH": str(copy / "src")}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"],
        cwd=copy, env=env, capture_output=True, text=True, timeout=600, check=False,
    )
    return done.returncode


def main() -> int:
    ignore = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.egg-info", "out", "run", ".venv")
    survived, unrun = [], []
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw) / "base"
        shutil.copytree(ROOT, base, ignore=ignore)
        baseline = run_pytest(base)
        print(f"baseline: pytest exit {baseline}")
        if baseline != 0:
            print("the suite must pass before any mutant means anything", file=sys.stderr)
            return 2
        for number, (name, relative, old, new) in enumerate(MUTANTS, start=1):
            copy = Path(raw) / f"m{number}"
            shutil.copytree(base, copy, ignore=ignore)
            target = copy / relative
            text = target.read_text(encoding="utf-8")
            if text.count(old) != 1:
                print(f"{number:>2} UNRUN     {name}: anchor occurs {text.count(old)} times in {relative}")
                unrun.append(name)
                continue
            before = md5(target)
            target.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
            if md5(target) == before:
                print(f"{number:>2} UNRUN     {name}: the file did not change")
                unrun.append(name)
                continue
            code = run_pytest(copy)
            if code == 1:
                print(f"{number:>2} killed    {name}")
            elif code == 0:
                print(f"{number:>2} SURVIVED  {name}")
                survived.append(name)
            else:
                print(f"{number:>2} UNRUN     {name}: pytest exit {code}")
                unrun.append(name)
            shutil.rmtree(copy, ignore_errors=True)
    killed = len(MUTANTS) - len(survived) - len(unrun)
    print(f"{len(MUTANTS)} mutants: {killed} killed, {len(survived)} survived, {len(unrun)} unrun")
    return 0 if not survived and not unrun else 1


if __name__ == "__main__":
    sys.exit(main())
