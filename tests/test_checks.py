import random
from dataclasses import replace
from datetime import date as calendar_date
from datetime import time

import pytest

from call_ledger_audit.checks import (
    RULES,
    attempt_cap,
    no_disclosure,
    opt_out_not_suppressed,
    orphan_writeback,
    outcome_mismatch,
    outside_window,
    promise_incomplete,
    run_checks,
    suppressed,
    writeback_deadline,
)
from call_ledger_audit.model import Attempt, Ledger, Policy, Suppression, Writeback, parse_timestamp

POLICY = Policy()
PROMISE_DATE = calendar_date(2026, 3, 6)


def ts(text):
    return parse_timestamp(text, "test")


def attempt(attempt_id="a1", when="2026-03-03T16:00:00Z", member="m1", phone="+13125550101",
            tz="America/Chicago", disposition="connected", disclosure=True):
    return Attempt(attempt_id, member, phone, tz, ts(when), disposition, disclosure)


def writeback(attempt_id="a1", when="2026-03-03T16:05:00Z", outcome="declined", amount=None, date=None):
    return Writeback(attempt_id, ts(when), outcome, amount, date)


def entry(member=None, phone=None, when="2026-03-03T10:00:00Z", source="dnc"):
    return Suppression(member, phone, ts(when), source)


def ledger(attempts=(), writebacks=(), suppressions=()):
    return Ledger(list(attempts), list(writebacks), list(suppressions))


def ids(findings):
    return [f.attempt_id for f in findings]


# outside_window: 16:00Z on 2026-03-03 is 10:00 in Chicago (CST, UTC-6)

@pytest.mark.parametrize(
    "when, flagged",
    [
        ("2026-03-03T14:00:00Z", False),  # 08:00:00 local, the first allowed second
        ("2026-03-03T13:59:59Z", True),   # 07:59:59
        ("2026-03-04T02:59:59Z", False),  # 20:59:59
        ("2026-03-04T03:00:00Z", True),   # 21:00:00, the end is outside
    ],
)
def test_window_boundaries(when, flagged):
    found = outside_window(ledger([attempt(when=when)]), POLICY)
    assert ids(found) == (["a1"] if flagged else [])


def test_window_uses_the_members_own_time_zone():
    # 06:30 in Los Angeles is 08:30 in Chicago: inside the window there, outside it here.
    la = attempt(tz="America/Los_Angeles", when="2026-03-03T14:30:00Z")
    assert ids(outside_window(ledger([la]), POLICY)) == ["a1"]


def test_window_follows_daylight_saving_in_both_directions():
    # 2026-03-08 clocks go forward: 13:30Z is 08:30 CDT. A fixed UTC-6 would call it 07:30.
    spring = attempt(when="2026-03-08T13:30:00Z")
    assert outside_window(ledger([spring]), POLICY) == []
    # 2026-11-01 clocks go back: 13:30Z is 07:30 CST. A fixed UTC-5 would call it 08:30.
    fall = attempt(when="2026-11-01T13:30:00Z")
    assert ids(outside_window(ledger([fall]), POLICY)) == ["a1"]


def test_window_without_daylight_saving_in_phoenix():
    # Phoenix stays at UTC-7: 15:30Z is 08:30 all year.
    for when in ("2026-01-15T15:30:00Z", "2026-07-15T15:30:00Z"):
        assert outside_window(ledger([attempt(tz="America/Phoenix", when=when)]), POLICY) == []


def test_window_comes_from_the_policy():
    policy = replace(POLICY, window_start=time(9, 0), window_end=time(17, 0))
    early = attempt(when="2026-03-03T14:59:59Z")  # 08:59:59
    assert ids(outside_window(ledger([early]), policy)) == ["a1"]
    assert "09:00 to 17:00" in outside_window(ledger([early]), policy)[0].detail


def test_window_finding_names_the_local_time():
    (finding,) = outside_window(ledger([attempt(when="2026-03-04T03:00:00Z")]), POLICY)
    assert "2026-03-03 21:00:00 America/Chicago" in finding.detail


# suppressed

def test_attempt_before_the_entry_is_clean_and_at_the_entry_is_not():
    entries = [entry(member="m1", when="2026-03-03T16:00:00Z")]
    before = attempt(when="2026-03-03T15:59:59Z")
    at_entry = attempt(attempt_id="a2", when="2026-03-03T16:00:00Z")
    after = attempt(attempt_id="a3", when="2026-03-05T16:00:00Z")
    found = suppressed(ledger([before, at_entry, after], suppressions=entries), POLICY)
    assert ids(found) == ["a2", "a3"]


def test_phone_only_entry_covers_another_member_on_the_same_phone():
    shared = attempt(member="m2", phone="+13125550199", when="2026-03-04T16:00:00Z")
    found = suppressed(ledger([shared], suppressions=[entry(phone="+13125550199")]), POLICY)
    assert ids(found) == ["a1"]


def test_member_only_entry_covers_a_new_phone():
    moved = attempt(member="m1", phone="+13125550777", when="2026-03-04T16:00:00Z")
    assert ids(suppressed(ledger([moved], suppressions=[entry(member="m1")]), POLICY)) == ["a1"]


def test_unrelated_member_is_not_suppressed():
    other = attempt(member="m9", phone="+13125550999", when="2026-03-04T16:00:00Z")
    entries = [entry(member="m1", phone="+13125550101")]
    assert suppressed(ledger([other], suppressions=entries), POLICY) == []


def test_suppression_detail_names_the_source():
    found = suppressed(
        ledger([attempt(when="2026-03-04T16:00:00Z")], suppressions=[entry(member="m1", source="opt_out_call")]),
        POLICY,
    )
    assert "opt_out_call entry added 2026-03-03T10:00:00Z" in found[0].detail


# no_disclosure

@pytest.mark.parametrize(
    "disposition, flagged",
    [("connected", True), ("opt_out", True), ("voicemail", False), ("no_answer", False)],
)
def test_disclosure_is_required_only_when_someone_spoke(disposition, flagged):
    found = no_disclosure(ledger([attempt(disposition=disposition, disclosure=False)]), POLICY)
    assert bool(found) is flagged


def test_disclosure_played_is_clean_and_the_policy_can_switch_it_off():
    assert no_disclosure(ledger([attempt(disclosure=True)]), POLICY) == []
    off = replace(POLICY, require_disclosure=False)
    assert no_disclosure(ledger([attempt(disclosure=False)]), off) == []


# attempt_cap: daily cap 2, 7-day cap 5

def day_attempts(count, day="2026-03-03", start_hour=15):
    return [attempt(attempt_id=f"{day}-{n}", when=f"{day}T{start_hour + n}:00:00Z") for n in range(count)]


def test_two_attempts_in_a_day_are_clean_and_every_attempt_past_the_cap_is_flagged():
    assert attempt_cap(ledger(day_attempts(2)), POLICY) == []
    found = attempt_cap(ledger(day_attempts(4)), POLICY)
    assert ids(found) == ["2026-03-03-2", "2026-03-03-3"]
    assert "3 attempts on 2026-03-03 (cap 2)" in found[0].detail


def test_the_day_is_the_members_local_day_not_the_utc_day():
    # 23:50, 00:10 and 00:20 Chicago time: one attempt on the 3rd, two on the 4th.
    rows = [
        attempt(attempt_id="a1", when="2026-03-04T05:50:00Z"),
        attempt(attempt_id="a2", when="2026-03-04T06:10:00Z"),
        attempt(attempt_id="a3", when="2026-03-04T06:20:00Z"),
    ]
    assert attempt_cap(ledger(rows), POLICY) == []


def week_attempts():
    return [
        attempt(attempt_id="d0a", when="2026-03-02T15:00:00Z"),
        attempt(attempt_id="d0b", when="2026-03-02T18:00:00Z"),
        attempt(attempt_id="d1a", when="2026-03-03T15:00:00Z"),
        attempt(attempt_id="d1b", when="2026-03-03T18:00:00Z"),
        attempt(attempt_id="d2a", when="2026-03-04T15:00:00Z"),
    ]


def test_five_in_seven_days_is_the_cap_and_the_sixth_is_flagged():
    five = week_attempts()
    assert attempt_cap(ledger(five), POLICY) == []
    sixth = attempt(attempt_id="d3a", when="2026-03-05T15:00:00Z")
    found = attempt_cap(ledger(five + [sixth]), POLICY)
    assert ids(found) == ["d3a"]
    assert "6 attempts in the 7 days" in found[0].detail


def test_an_attempt_exactly_seven_days_earlier_does_not_count():
    five = week_attempts()
    exactly = attempt(attempt_id="late", when="2026-03-09T15:00:00Z")  # d0a plus 7 days
    assert attempt_cap(ledger(five + [exactly]), POLICY) == []
    inside = attempt(attempt_id="late", when="2026-03-09T14:59:59Z")  # one second short of 7 days
    assert ids(attempt_cap(ledger(five + [inside]), POLICY)) == ["late"]


def test_cap_does_not_depend_on_row_order_or_mix_members():
    rows = day_attempts(3) + [attempt(attempt_id="other", member="m2", when="2026-03-03T15:00:00Z")]
    expected = ids(attempt_cap(ledger(rows), POLICY))
    assert expected == ["2026-03-03-2"]
    for seed in range(5):
        shuffled = rows[:]
        random.Random(seed).shuffle(shuffled)
        assert ids(attempt_cap(ledger(shuffled), POLICY)) == expected


def test_caps_come_from_the_policy():
    one = replace(POLICY, max_attempts_per_day=1)
    assert ids(attempt_cap(ledger(day_attempts(2)), one)) == ["2026-03-03-1"]
    three_a_week = replace(POLICY, max_attempts_per_7_days=3, max_attempts_per_day=9)
    assert ids(attempt_cap(ledger(week_attempts()), three_a_week)) == ["d1b", "d2a"]


# writeback_deadline: 30 minutes

def test_connected_call_without_a_writeback_is_flagged():
    found = writeback_deadline(ledger([attempt()]), POLICY)
    assert ids(found) == ["a1"] and found[0].detail == "no write-back"


def test_writeback_exactly_at_the_deadline_is_on_time_and_one_second_later_is_not():
    on_time = writeback(when="2026-03-03T16:30:00Z")
    late = writeback(when="2026-03-03T16:30:01Z")
    assert writeback_deadline(ledger([attempt()], [on_time]), POLICY) == []
    assert ids(writeback_deadline(ledger([attempt()], [late]), POLICY)) == ["a1"]


def test_the_earliest_writeback_counts():
    rows = [writeback(when="2026-03-03T17:00:00Z"), writeback(when="2026-03-03T16:10:00Z")]
    assert writeback_deadline(ledger([attempt()], rows), POLICY) == []


def test_only_the_policys_dispositions_need_a_writeback():
    quiet = attempt(disposition="no_answer", disclosure=False)
    assert writeback_deadline(ledger([quiet]), POLICY) == []
    everything = replace(POLICY, writeback_dispositions=("connected", "opt_out", "no_answer"))
    assert ids(writeback_deadline(ledger([quiet]), everything)) == ["a1"]


def test_deadline_comes_from_the_policy():
    policy = replace(POLICY, writeback_deadline_minutes=2)
    assert ids(writeback_deadline(ledger([attempt()], [writeback(when="2026-03-03T16:03:00Z")]), policy)) == ["a1"]


# orphan_writeback

def test_writeback_for_an_unknown_attempt_is_flagged():
    rows = [writeback(attempt_id="ghost", outcome="no_contact"), writeback(attempt_id="a1")]
    found = orphan_writeback(ledger([attempt()], rows), POLICY)
    assert ids(found) == ["ghost"] and found[0].member_id is None


# outcome_mismatch

@pytest.mark.parametrize(
    "disposition, outcome, flagged",
    [
        ("voicemail", "promise_to_pay", True),
        ("no_answer", "declined", True),
        ("connected", "no_contact", True),
        ("connected", "opt_out", True),
        ("opt_out", "declined", True),
        ("connected", "promise_to_pay", False),
        ("connected", "callback_requested", False),
        ("connected", "declined", False),
        ("opt_out", "opt_out", False),
        ("voicemail", "no_contact", False),
        ("no_answer", "no_contact", False),
    ],
)
def test_outcome_must_be_one_the_disposition_can_produce(disposition, outcome, flagged):
    rows = ledger([attempt(disposition=disposition)], [writeback(outcome=outcome)])
    assert bool(outcome_mismatch(rows, POLICY)) is flagged


def test_two_writebacks_that_disagree_are_flagged_and_two_that_agree_are_not():
    disagree = [writeback(outcome="declined"), writeback(when="2026-03-03T16:20:00Z", outcome="callback_requested")]
    agree = [writeback(outcome="declined"), writeback(when="2026-03-03T16:20:00Z", outcome="declined")]
    found = outcome_mismatch(ledger([attempt()], disagree), POLICY)
    assert ids(found) == ["a1"] and "write-backs disagree: callback_requested, declined" in found[0].detail
    assert outcome_mismatch(ledger([attempt()], agree), POLICY) == []


def test_an_attempt_with_no_writeback_is_not_an_outcome_mismatch():
    assert outcome_mismatch(ledger([attempt()]), POLICY) == []


# promise_incomplete

UNSET = object()


def promise(amount=UNSET, date=UNSET, attempt_id="a1"):
    return writeback(
        attempt_id=attempt_id,
        outcome="promise_to_pay",
        amount=125.0 if amount is UNSET else amount,
        date=PROMISE_DATE if date is UNSET else date,
    )


@pytest.mark.parametrize(
    "kwargs, detail",
    [
        ({"amount": None}, "no amount"),
        ({"amount": 0}, "amount 0"),
        ({"amount": -5.0}, "amount -5.0"),
        ({"date": None}, "no date"),
        ({"amount": None, "date": None}, "no amount, no date"),
    ],
)
def test_incomplete_promises_are_flagged_with_the_reason(kwargs, detail):
    found = promise_incomplete(ledger([attempt()], [promise(**kwargs)]), POLICY)
    assert ids(found) == ["a1"] and found[0].detail == detail


def test_complete_promise_and_other_outcomes_are_clean():
    assert promise_incomplete(ledger([attempt()], [promise()]), POLICY) == []
    assert promise_incomplete(ledger([attempt()], [writeback(outcome="declined")]), POLICY) == []


def test_an_orphan_promise_is_still_checked():
    found = promise_incomplete(ledger([], [promise(amount=None, attempt_id="ghost")]), POLICY)
    assert ids(found) == ["ghost"] and found[0].member_id is None


# opt_out_not_suppressed: 60 minutes

OPT_OUT = dict(disposition="opt_out", when="2026-03-03T16:00:00Z")


def test_opt_out_with_no_entry_is_flagged():
    found = opt_out_not_suppressed(ledger([attempt(**OPT_OUT)]), POLICY)
    assert ids(found) == ["a1"] and "no suppression entry by 2026-03-03T17:00:00Z" in found[0].detail


def test_entry_exactly_at_the_deadline_is_on_time_and_one_second_later_is_not():
    on_time = entry(member="m1", when="2026-03-03T17:00:00Z", source="opt_out_call")
    late = entry(member="m1", when="2026-03-03T17:00:01Z", source="opt_out_call")
    assert opt_out_not_suppressed(ledger([attempt(**OPT_OUT)], suppressions=[on_time]), POLICY) == []
    assert ids(opt_out_not_suppressed(ledger([attempt(**OPT_OUT)], suppressions=[late]), POLICY)) == ["a1"]


def test_an_entry_from_before_the_call_counts_and_a_phone_only_entry_counts():
    earlier = entry(member="m1", when="2026-03-01T10:00:00Z")
    by_phone = entry(phone="+13125550101", when="2026-03-03T16:10:00Z", source="opt_out_call")
    assert opt_out_not_suppressed(ledger([attempt(**OPT_OUT)], suppressions=[earlier]), POLICY) == []
    assert opt_out_not_suppressed(ledger([attempt(**OPT_OUT)], suppressions=[by_phone]), POLICY) == []


def test_only_opt_out_calls_are_checked_here():
    assert opt_out_not_suppressed(ledger([attempt()]), POLICY) == []


# run_checks

def test_a_clean_ledger_has_no_findings():
    rows = ledger(
        [attempt(), attempt(attempt_id="a2", disposition="no_answer", disclosure=False, when="2026-03-04T16:00:00Z")],
        [writeback()],
    )
    assert run_checks(rows, POLICY) == []


def test_findings_come_back_ordered_by_attempt_id_then_rule():
    rows = ledger(
        [attempt(attempt_id="b", when="2026-03-03T13:00:00Z", disclosure=False), attempt(attempt_id="a", when="2026-03-03T13:00:00Z")]
    )
    found = run_checks(rows, POLICY)
    assert [(f.attempt_id, f.rule) for f in found] == [
        ("a", "outside_window"),
        ("a", "writeback_deadline"),
        ("b", "no_disclosure"),
        ("b", "outside_window"),
        ("b", "writeback_deadline"),
    ]


def test_every_rule_reports_under_its_own_name():
    assert list(RULES) == [
        "outside_window", "suppressed", "no_disclosure", "attempt_cap", "writeback_deadline",
        "orphan_writeback", "outcome_mismatch", "promise_incomplete", "opt_out_not_suppressed",
    ]
    for name, check in RULES.items():
        assert check.__name__ == name
