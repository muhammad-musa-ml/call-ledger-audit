import random
from collections import Counter
from datetime import time, timedelta
from zoneinfo import ZoneInfo

import pytest

from call_ledger_audit.checks import RULES, Finding, run_checks
from call_ledger_audit.cli import main
from call_ledger_audit.generate import PLANTED, generate, score, write_dataset
from call_ledger_audit.model import Ledger, load_ledger

FILES = ("attempts.jsonl", "writebacks.jsonl", "suppressions.jsonl", "policy.json", "planted.json")
SEEDS = range(1, 21)  # the seeds scripts/reproduce.py runs; seed 19 once drew a 0-second opt-out delay


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


def test_the_boundary_cases_are_there_on_their_own_with_no_random_traffic():
    """members=0 leaves only the deliberate boundary calls, so nothing here can be satisfied by chance."""
    dataset = generate(2, members=0, plant=False)
    attempts = dataset.ledger.attempts
    assert run_checks(dataset.ledger, dataset.policy) == []
    clocks = {(local(a).time(), a.tz) for a in attempts}
    assert (time(8, 0, 0), "America/Chicago") in clocks and (time(20, 59, 59), "America/Los_Angeles") in clocks
    # Phoenix at 20:30 in July: inside there, and one hour past the close in a zone with daylight saving.
    phoenix = [a for a in attempts if a.tz == "America/Phoenix"]
    assert [local(a).time() for a in phoenix] == [time(20, 30, 0)]
    assert local(phoenix[0]).date().isoformat() == "2026-07-15"
    # Both clock-change days, at both ends of the window, in Chicago.
    chicago = sorted((local(a).date().isoformat(), local(a).time()) for a in attempts if a.tz == "America/Chicago")
    for day in ("2026-03-08", "2026-11-01"):
        assert len([1 for d, _ in chicago if d == day]) == 2
    assert ("2026-03-08", time(8, 30)) in chicago and ("2026-03-08", time(20, 30)) in chicago
    assert ("2026-11-01", time(8, 15)) in chicago and ("2026-11-01", time(20, 45)) in chicago
    by_attempt = {a.attempt_id: a for a in attempts}
    delays = {w.written_at - by_attempt[w.attempt_id].started_at for w in dataset.ledger.writebacks}
    assert timedelta(minutes=30) in delays
    entry_delays = {
        s.added_at - by_attempt[s.attempt_id].started_at for s in dataset.ledger.suppressions if s.attempt_id
    }
    assert timedelta(minutes=60) in entry_delays and timedelta(0) in entry_delays
    per_member = Counter(a.member_id for a in attempts)
    assert max(per_member.values()) == 6  # five in three days, then one exactly 7 days after the first
    sixth = [a for a in attempts if per_member[a.member_id] == 6]
    first, last = min(sixth, key=lambda a: a.started_at), max(sixth, key=lambda a: a.started_at)
    assert last.started_at - first.started_at == timedelta(days=7)


def test_the_generated_phone_numbers_cannot_be_real_ones():
    dataset = generate(3, members=20)
    assert {a.phone[:8] for a in dataset.ledger.attempts} == {"+1000555"}


def test_a_label_names_an_attempt_in_the_ledger_or_an_id_that_is_not_there_for_an_orphan():
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
    with pytest.raises(ValueError, match="members must be 0 to"):
        generate(1, members=-1)
    with pytest.raises(ValueError, match="reps 1 to"):
        generate(1, reps=21)


def test_score_reports_what_was_missed_and_what_was_reported_on_the_wrong_attempt():
    """score() is what the results table rests on, so show that it can say something other than 'none'."""
    planted = [("outside_window", "a1", "v"), ("suppressed", "a2", "v")]
    right = [Finding("outside_window", "a1", "m1", ""), Finding("suppressed", "a2", "m2", "")]
    assert score(right, planted) == (set(), set())
    wrong = [Finding("outside_window", "a1", "m1", ""), Finding("suppressed", "a3", "m3", ""),
             Finding("no_disclosure", "a1", "m1", "")]
    missed, extra = score(wrong, planted)
    assert missed == {("suppressed", "a2")}
    assert extra == {("suppressed", "a3"), ("no_disclosure", "a1")}


def test_generate_then_check_from_the_command_line(tmp_path, capsys):
    assert main(["generate", "--seed", "11", "--members", "30", "--out", str(tmp_path)]) == 0
    assert "planted defects listed in planted.json" in capsys.readouterr().out
    assert main(["check", "--dir", str(tmp_path), "--json", str(tmp_path / "report.json")]) == 1
    capsys.readouterr()
    assert main(["generate", "--seed", "11", "--members", "30", "--no-plant", "--out", str(tmp_path / "clean")]) == 0
    capsys.readouterr()
    assert main(["check", "--dir", str(tmp_path / "clean")]) == 0
    assert main(["generate", "--seed", "1", "--members=-1", "--out", str(tmp_path / "bad")]) == 2
