import json
import os
import subprocess
import sys
from pathlib import Path

from call_ledger_audit.cli import main

SRC = str(Path(__file__).resolve().parents[1] / "src")

ATTEMPT = {
    "attempt_id": "a1", "member_id": "m1", "phone": "+13125550101", "tz": "America/Chicago",
    "started_at": "2026-03-03T16:00:00Z", "disposition": "connected", "disclosure_played": True,
}
WRITEBACK = {"attempt_id": "a1", "written_at": "2026-03-03T16:05:00Z", "outcome": "declined"}


def write_dir(directory, attempts, writebacks=(), suppressions=(), policy=None):
    def put(name, rows):
        (directory / name).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    put("attempts.jsonl", attempts)
    put("writebacks.jsonl", writebacks)
    put("suppressions.jsonl", suppressions)
    if policy is not None:
        (directory / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    return directory


def test_clean_directory_exits_0(tmp_path, capsys):
    write_dir(tmp_path, [ATTEMPT], [WRITEBACK])
    assert main(["check", "--dir", str(tmp_path)]) == 0
    assert "1 attempts, 1 write-backs, 0 suppression entries: 0 finding(s)" in capsys.readouterr().out


def test_a_finding_exits_1_prints_it_and_writes_the_report(tmp_path, capsys):
    write_dir(tmp_path, [{**ATTEMPT, "started_at": "2026-03-04T03:00:00Z"}], [WRITEBACK])
    report_path = tmp_path / "report.json"
    assert main(["check", "--dir", str(tmp_path), "--json", str(report_path)]) == 1
    out = capsys.readouterr().out
    assert "outside_window" in out and "a1" in out and "by rule: outside_window 1" in out
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["counts"]["outside_window"] == 1 and report["counts"]["suppressed"] == 0
    assert report["findings"][0]["attempt_id"] == "a1"


def test_the_policy_file_in_the_directory_is_used(tmp_path):
    write_dir(tmp_path, [ATTEMPT], [WRITEBACK], policy={"window_start": "11:00"})
    assert main(["check", "--dir", str(tmp_path)]) == 1  # 10:00 local is before an 11:00 start


def test_a_bad_line_exits_2_and_names_the_file_and_line(tmp_path, capsys):
    write_dir(tmp_path, [ATTEMPT, {**ATTEMPT, "attempt_id": "a2", "tz": "Mars/Olympus"}], [WRITEBACK])
    assert main(["check", "--dir", str(tmp_path)]) == 2
    assert "attempts.jsonl line 2: unknown time zone" in capsys.readouterr().err


def test_a_missing_export_exits_2(tmp_path, capsys):
    assert main(["check", "--dir", str(tmp_path)]) == 2
    assert "attempts.jsonl: file not found" in capsys.readouterr().err


def test_trace_prints_the_members_history_in_time_order_with_findings(tmp_path, capsys):
    attempts = [
        {**ATTEMPT, "attempt_id": "a2", "started_at": "2026-03-05T16:00:00Z", "disposition": "no_answer", "disclosure_played": False},
        ATTEMPT,
        {**ATTEMPT, "attempt_id": "other", "member_id": "m2", "phone": "+13125550102"},
    ]
    writebacks = [{**WRITEBACK, "outcome": "promise_to_pay", "promise_amount": 125, "promise_date": "2026-03-06"}]
    suppressions = [{"member_id": "m1", "added_at": "2026-03-04T12:00:00Z", "source": "dnc"}]
    write_dir(tmp_path, attempts, writebacks, suppressions)
    assert main(["trace", "--dir", str(tmp_path), "--member", "m1"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "member m1, phone +13125550101",
        "2026-03-03 10:00:00 CST  16:00:00Z  call a1  connected, disclosure played",
        "2026-03-03 10:05:00 CST  16:05:00Z  write-back for a1  promise_to_pay amount=125 date=2026-03-06",
        "2026-03-04 06:00:00 CST  12:00:00Z  suppression entry  dnc",
        "2026-03-05 10:00:00 CST  16:00:00Z  call a2  no_answer, no disclosure",
        "    ! suppressed: dnc entry added 2026-03-04T12:00:00Z, attempt began 2026-03-05T16:00:00Z",
    ]


def test_trace_of_an_unknown_member_exits_2(tmp_path, capsys):
    write_dir(tmp_path, [ATTEMPT], [WRITEBACK])
    assert main(["trace", "--dir", str(tmp_path), "--member", "nobody"]) == 2
    assert "no attempts for member 'nobody'" in capsys.readouterr().err


def test_installed_entry_point_style_run_returns_the_exit_code(tmp_path):
    write_dir(tmp_path, [{**ATTEMPT, "started_at": "2026-03-04T03:00:00Z"}], [WRITEBACK])
    env = {**os.environ, "PYTHONPATH": SRC}
    done = subprocess.run(
        [sys.executable, "-m", "call_ledger_audit", "check", "--dir", str(tmp_path)],
        capture_output=True, text=True, env=env, check=False,
    )
    assert done.returncode == 1 and "outside_window" in done.stdout
