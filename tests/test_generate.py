import random
from collections import Counter
from datetime import time, timedelta
from zoneinfo import ZoneInfo

import pytest

from call_ledger_audit.checks import RULES, run_checks
from call_ledger_audit.cli import main
from call_ledger_audit.generate import PLANTED, generate, score, write_dataset
from call_ledger_audit.model import Ledger, load_ledger

FILES = ("attempts.jsonl", "writebacks.jsonl", "suppressions.jsonl", "policy.json", "planted.json")
SEEDS = range(1, 11)


def local(attempt):
    return attempt.started_at.astimezone(ZoneInfo(attempt.tz))


def test_the_same_seed_gives_the_same_bytes_and_another_seed_does_not(tmp_path):
    for name, seed in (("one", 7), ("two", 7), ("three", 8)):
        write_dataset(generate(seed, members=40), tmp_path / name)
    for file in FILES:
        assert (tmp_path / "one" / file).read_bytes() == (tmp_path / "two" / file).read_bytes()
    assert (tmp_path / "one" / "attempts.jsonl").read_bytes() != (tmp_path / "three" / "attempts.jsonl").read_bytes()


@pytest.mark.parametrize("seed", SEEDS)
def test_the_clean_dataset_has_no_findings(seed):
    dataset = generate(seed, plant=False)
    assert dataset.planted == []
    assert run_checks(dataset.ledger, dataset.policy) == []


@pytest.mark.parametrize("seed", SEEDS)
def test_planted_findings_match_the_labels_exactly(seed):
    dataset = generate(seed)
    findings = run_checks(dataset.ledger, dataset.policy)
    missed, extra = score(findings, dataset.planted)
    assert missed == set() and extra == set()
    assert len(findings) == len(dataset.planted)


def test_every_variant_is_planted_reps_times_and_every_rule_is_covered():
    dataset = generate(3, members=20, reps=2)
    seen = Counter((rule, variant) for rule, _, variant in dataset.planted)
    expected = {(rule, variant): 2 for rule, variants in PLANTED.items() for variant in variants}
    assert seen == expected
    assert set(PLANTED) == set(RULES)


def test_findings_do_not_depend_on_row_order():
    dataset = generate(5)
    ledger = dataset.ledger
    shuffled = Ledger(ledger.attempts[:], ledger.writebacks[:], ledger.suppressions[:])
    for rows in (shuffled.attempts, shuffled.writebacks, shuffled.suppressions):
        random.Random(1).shuffle(rows)
    assert run_checks(shuffled, dataset.policy) == run_checks(ledger, dataset.policy)


def test_the_files_load_back_to_the_same_ledger(tmp_path):
    dataset = generate(4, members=60)
    write_dataset(dataset, tmp_path)
    loaded, policy = load_ledger(tmp_path)
    assert loaded == dataset.ledger
    assert policy == dataset.policy


def test_the_clean_part_really_holds_the_boundary_cases():
    """Without these the clean dataset would pass any check that is merely lenient."""
    dataset = generate(2, plant=False)
    attempts = dataset.ledger.attempts
    clocks = {local(a).time() for a in attempts}
    assert time(8, 0, 0) in clocks and time(20, 59, 59) in clocks
    days = {(local(a).date().isoformat(), a.tz) for a in attempts}
    assert ("2026-03-08", "America/Chicago") in days and ("2026-11-01", "America/Chicago") in days
    by_attempt = {a.attempt_id: a for a in attempts}
    delays = {w.written_at - by_attempt[w.attempt_id].started_at for w in dataset.ledger.writebacks}
    assert timedelta(minutes=30) in delays
    opt_outs = {a.attempt_id: a for a in attempts if a.disposition == "opt_out"}
    entry_delays = {
        s.added_at - a.started_at for s in dataset.ledger.suppressions for a in opt_outs.values()
        if s.member_id == a.member_id
    }
    assert timedelta(minutes=60) in entry_delays
    per_member = Counter(a.member_id for a in attempts)
    assert max(per_member.values()) == 6  # five in three days, then one exactly 7 days after the first


def test_a_planted_defect_is_reported_on_the_attempt_the_label_names():
    dataset = generate(6, members=10, reps=1)
    by_id = {a.attempt_id: a for a in dataset.ledger.attempts}
    for rule, attempt_id, variant in dataset.planted:
        if rule == "orphan_writeback":
            assert attempt_id not in by_id
        else:
            assert attempt_id in by_id, (rule, variant)


def test_each_variant_trips_only_its_own_rule():
    dataset = generate(9, members=10, reps=1)
    rules_by_attempt = {}
    for finding in run_checks(dataset.ledger, dataset.policy):
        rules_by_attempt.setdefault(finding.attempt_id, set()).add(finding.rule)
    for rule, attempt_id, variant in dataset.planted:
        assert rules_by_attempt[attempt_id] == {rule}, (rule, variant)


def test_size_limits():
    with pytest.raises(ValueError, match="members must be 1 to"):
        generate(1, members=0)
    with pytest.raises(ValueError, match="reps 1 to"):
        generate(1, reps=21)


def test_generate_then_check_from_the_command_line(tmp_path, capsys):
    assert main(["generate", "--seed", "11", "--members", "30", "--out", str(tmp_path)]) == 0
    assert "planted defects listed in planted.json" in capsys.readouterr().out
    assert main(["check", "--dir", str(tmp_path), "--json", str(tmp_path / "report.json")]) == 1
    capsys.readouterr()
    assert main(["generate", "--seed", "11", "--members", "30", "--no-plant", "--out", str(tmp_path / "clean")]) == 0
    capsys.readouterr()
    assert main(["check", "--dir", str(tmp_path / "clean")]) == 0
    assert main(["generate", "--seed", "1", "--members", "0", "--out", str(tmp_path / "bad")]) == 2
