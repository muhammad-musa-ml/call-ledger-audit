import json
from datetime import date, time, timezone

import pytest

from call_ledger_audit.model import (
    InputError,
    Policy,
    load_attempts,
    load_policy,
    load_suppressions,
    load_writebacks,
    parse_timestamp,
)

GOOD_ATTEMPT = {
    "attempt_id": "a1",
    "member_id": "m1",
    "phone": "+13125550101",
    "tz": "America/Chicago",
    "started_at": "2026-03-02T15:04:05Z",
    "disposition": "connected",
    "disclosure_played": True,
}


def write_lines(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_timestamp_z_and_offset_give_the_same_instant():
    z = parse_timestamp("2026-03-02T15:04:05Z", "t")
    offset = parse_timestamp("2026-03-02T09:04:05-06:00", "t")
    assert z == offset
    assert z.tzinfo is timezone.utc or z.utcoffset().total_seconds() == 0


def test_timestamp_without_offset_is_refused():
    with pytest.raises(InputError, match="no Z or offset"):
        parse_timestamp("2026-03-02T15:04:05", "t")


def test_timestamp_garbage_is_refused():
    with pytest.raises(InputError, match="not an ISO 8601"):
        parse_timestamp("yesterday", "t")
    with pytest.raises(InputError, match="must be a string"):
        parse_timestamp(1772463845, "t")


def test_attempt_loads(tmp_path):
    path = write_lines(tmp_path / "attempts.jsonl", [GOOD_ATTEMPT])
    (attempt,) = load_attempts(path)
    assert attempt.attempt_id == "a1"
    assert attempt.started_at.isoformat() == "2026-03-02T15:04:05+00:00"


def test_blank_lines_are_skipped(tmp_path):
    path = tmp_path / "attempts.jsonl"
    path.write_text("\n" + json.dumps(GOOD_ATTEMPT) + "\n\n", encoding="utf-8")
    assert len(load_attempts(path)) == 1


@pytest.mark.parametrize(
    "change, message",
    [
        ({"disposition": "answered"}, "disposition must be one of"),
        ({"tz": "Mars/Olympus"}, "unknown time zone"),
        ({"disclosure_played": "yes"}, "disclosure_played must be true or false"),
        ({"member_id": ""}, "member_id must be a non-empty string"),
        ({"started_at": "2026-03-02 15:04"}, "no Z or offset"),
    ],
)
def test_attempt_rejects_bad_fields(tmp_path, change, message):
    path = write_lines(tmp_path / "attempts.jsonl", [{**GOOD_ATTEMPT, **change}])
    with pytest.raises(InputError, match=message):
        load_attempts(path)


def test_duplicate_attempt_id_is_refused_and_names_the_line(tmp_path):
    path = write_lines(tmp_path / "attempts.jsonl", [GOOD_ATTEMPT, GOOD_ATTEMPT])
    with pytest.raises(InputError, match=r"attempts.jsonl line 2: attempt_id 'a1' appears twice"):
        load_attempts(path)


def test_invalid_json_names_the_line(tmp_path):
    path = tmp_path / "attempts.jsonl"
    path.write_text(json.dumps(GOOD_ATTEMPT) + "\n{oops\n", encoding="utf-8")
    with pytest.raises(InputError, match="line 2: not valid JSON"):
        load_attempts(path)


def test_missing_file_is_an_input_error(tmp_path):
    with pytest.raises(InputError, match="file not found"):
        load_attempts(tmp_path / "nope.jsonl")


def test_writeback_loads_amount_and_date(tmp_path):
    row = {
        "attempt_id": "a1",
        "written_at": "2026-03-02T15:10:00Z",
        "outcome": "promise_to_pay",
        "promise_amount": 125.5,
        "promise_date": "2026-03-06",
    }
    (wb,) = load_writebacks(write_lines(tmp_path / "w.jsonl", [row]))
    assert wb.promise_amount == 125.5
    assert wb.promise_date == date(2026, 3, 6)


@pytest.mark.parametrize(
    "change, message",
    [
        ({"outcome": "paid"}, "outcome must be one of"),
        ({"promise_amount": "125"}, "promise_amount must be a number or null"),
        ({"promise_amount": True}, "promise_amount must be a number or null"),
        ({"promise_date": "03/06/2026"}, "promise_date is not YYYY-MM-DD"),
        ({"promise_date": 20260306}, "promise_date must be YYYY-MM-DD or null"),
    ],
)
def test_writeback_rejects_bad_fields(tmp_path, change, message):
    row = {"attempt_id": "a1", "written_at": "2026-03-02T15:10:00Z", "outcome": "declined", **change}
    with pytest.raises(InputError, match=message):
        load_writebacks(write_lines(tmp_path / "w.jsonl", [row]))


def test_suppression_needs_a_member_or_a_phone(tmp_path):
    row = {"added_at": "2026-03-02T15:10:00Z", "source": "dnc"}
    with pytest.raises(InputError, match="needs a member_id, a phone or both"):
        load_suppressions(write_lines(tmp_path / "s.jsonl", [row]))
    ok = {**row, "phone": "+13125550101"}
    (entry,) = load_suppressions(write_lines(tmp_path / "s.jsonl", [ok]))
    assert entry.member_id is None and entry.phone == "+13125550101"


def test_suppression_may_name_the_call_that_produced_it(tmp_path):
    row = {"member_id": "m1", "added_at": "2026-03-02T15:10:00Z", "source": "opt_out_call", "attempt_id": "a1"}
    (entry,) = load_suppressions(write_lines(tmp_path / "s.jsonl", [row]))
    assert entry.attempt_id == "a1"
    (nameless,) = load_suppressions(write_lines(tmp_path / "s.jsonl", [{**row, "attempt_id": None}]))
    assert nameless.attempt_id is None
    with pytest.raises(InputError, match="attempt_id must be a non-empty string"):
        load_suppressions(write_lines(tmp_path / "s.jsonl", [{**row, "attempt_id": 7}]))


def test_policy_defaults_when_no_file():
    assert load_policy(None) == Policy()


def test_policy_overrides_and_keeps_the_rest(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"window_start": "09:00", "max_attempts_per_day": 1}), encoding="utf-8")
    policy = load_policy(path)
    assert policy.window_start == time(9, 0)
    assert policy.window_end == time(21, 0)
    assert policy.max_attempts_per_day == 1
    assert policy.max_attempts_per_7_days == 5


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"window_strat": "09:00"}, "unknown key"),
        ({"window_start": "21:00", "window_end": "08:00"}, "window_start must be earlier"),
        ({"window_start": "9am"}, "must look like 08:00"),
        ({"max_attempts_per_day": 0}, "at least 1"),
        ({"max_attempts_per_day": True}, "at least 1"),
        ({"writeback_deadline_minutes": -1}, "at least 0"),
        ({"require_disclosure": "yes"}, "true or false"),
        ({"writeback_dispositions": ["connected", "ringing"]}, "writeback_dispositions must be a list"),
    ],
)
def test_policy_rejects_bad_values(tmp_path, raw, message):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(InputError, match=message):
        load_policy(path)
