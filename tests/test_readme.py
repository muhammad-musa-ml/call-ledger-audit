"""The README quotes a results table and a trace printout. Keep both equal to what the code produces."""

import subprocess
import sys
from pathlib import Path

from call_ledger_audit.cli import main

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
SUMMARY = (ROOT / "results" / "summary.md").read_text(encoding="utf-8").splitlines()


def test_the_readme_table_is_the_committed_results_table():
    table = [line for line in SUMMARY if line.startswith("|")]
    assert len(table) == 12  # header, separator, nine rules, the total
    for line in table:
        assert line in README, line


def test_the_committed_results_are_what_the_script_produces_now():
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "reproduce.py"), "--check"], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr


def test_the_readme_trace_is_what_trace_prints(tmp_path, capsys):
    start = next(i for i, line in enumerate(README) if line.startswith("member m0195"))
    end = next(i for i in range(start, len(README)) if README[i].startswith("```"))
    quoted = README[start:end]
    assert main(["generate", "--seed", "1", "--out", str(tmp_path)]) == 0
    capsys.readouterr()
    assert main(["trace", "--dir", str(tmp_path), "--member", "m0195"]) == 0
    assert capsys.readouterr().out.splitlines() == quoted
