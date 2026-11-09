"""The four input files, loaded and validated.

attempts.jsonl      one line per dial attempt
writebacks.jsonl    one line per outcome the agent wrote to the system of record
suppressions.jsonl  one line per do-not-call or opt-out entry
policy.json         the limits the checks use (optional, every key has a default)

Timestamps are ISO 8601 with a "Z" or an offset. A timestamp without one is
refused, because every check compares instants and a bare time has none.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DISPOSITIONS = ("no_answer", "voicemail", "connected", "opt_out")
OUTCOMES = ("promise_to_pay", "callback_requested", "declined", "opt_out", "no_contact")
SUPPRESSION_SOURCES = ("dnc", "opt_out_call")


class InputError(Exception):
    """An input file is malformed. The command line exits 2 on it."""


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    member_id: str
    phone: str
    tz: str
    started_at: datetime
    disposition: str
    disclosure_played: bool


@dataclass(frozen=True)
class Writeback:
    attempt_id: str
    written_at: datetime
    outcome: str
    promise_amount: float | None
    promise_date: date | None


@dataclass(frozen=True)
class Suppression:
    member_id: str | None
    phone: str | None
    added_at: datetime
    source: str
    attempt_id: str | None = None  # the call that produced the entry, when the export says so


@dataclass(frozen=True)
class Policy:
    window_start: time = time(8, 0)
    window_end: time = time(21, 0)
    max_attempts_per_day: int = 2
    max_attempts_per_7_days: int = 5
    writeback_deadline_minutes: int = 30
    suppression_deadline_minutes: int = 60
    require_disclosure: bool = True
    writeback_dispositions: tuple[str, ...] = ("connected", "opt_out")


@dataclass
class Ledger:
    attempts: list[Attempt] = field(default_factory=list)
    writebacks: list[Writeback] = field(default_factory=list)
    suppressions: list[Suppression] = field(default_factory=list)


def _fail(where: str, message: str) -> InputError:
    return InputError(f"{where}: {message}")


def parse_timestamp(value: object, where: str) -> datetime:
    if not isinstance(value, str):
        raise _fail(where, f"timestamp must be a string, got {type(value).__name__}")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise _fail(where, f"not an ISO 8601 timestamp: {value!r}") from None
    if parsed.tzinfo is None:
        raise _fail(where, f"timestamp has no Z or offset: {value!r}")
    return parsed.astimezone(timezone.utc)


def _text(row: dict, key: str, where: str, *, optional: bool = False) -> str | None:
    value = row.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise _fail(where, f"{key} must be a non-empty string")
    return value


def _choice(row: dict, key: str, allowed: tuple[str, ...], where: str) -> str:
    value = _text(row, key, where)
    if value not in allowed:
        raise _fail(where, f"{key} must be one of {', '.join(allowed)}, got {value!r}")
    return value


def _rows(path: Path):
    if not path.is_file():
        raise InputError(f"{path}: file not found")
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            where = f"{path.name} line {number}"
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise _fail(where, f"not valid JSON ({error.msg})") from None
            if not isinstance(row, dict):
                raise _fail(where, "each line must be a JSON object")
            yield row, where


def load_attempts(path: Path) -> list[Attempt]:
    attempts: list[Attempt] = []
    seen: set[str] = set()
    for row, where in _rows(path):
        attempt_id = _text(row, "attempt_id", where)
        if attempt_id in seen:
            raise _fail(where, f"attempt_id {attempt_id!r} appears twice")
        seen.add(attempt_id)
        tz = _text(row, "tz", where)
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise _fail(where, f"unknown time zone {tz!r}") from None
        disclosure = row.get("disclosure_played")
        if not isinstance(disclosure, bool):
            raise _fail(where, "disclosure_played must be true or false")
        attempts.append(
            Attempt(
                attempt_id=attempt_id,
                member_id=_text(row, "member_id", where),
                phone=_text(row, "phone", where),
                tz=tz,
                started_at=parse_timestamp(row.get("started_at"), f"{where} started_at"),
                disposition=_choice(row, "disposition", DISPOSITIONS, where),
                disclosure_played=disclosure,
            )
        )
    return attempts


def load_writebacks(path: Path) -> list[Writeback]:
    writebacks: list[Writeback] = []
    for row, where in _rows(path):
        amount = row.get("promise_amount")
        if amount is not None and (isinstance(amount, bool) or not isinstance(amount, (int, float))):
            raise _fail(where, "promise_amount must be a number or null")
        raw_date = row.get("promise_date")
        promise_date = None
        if raw_date is not None:
            if not isinstance(raw_date, str):
                raise _fail(where, "promise_date must be YYYY-MM-DD or null")
            try:
                promise_date = date.fromisoformat(raw_date)
            except ValueError:
                raise _fail(where, f"promise_date is not YYYY-MM-DD: {raw_date!r}") from None
        writebacks.append(
            Writeback(
                attempt_id=_text(row, "attempt_id", where),
                written_at=parse_timestamp(row.get("written_at"), f"{where} written_at"),
                outcome=_choice(row, "outcome", OUTCOMES, where),
                promise_amount=amount,
                promise_date=promise_date,
            )
        )
    return writebacks


def load_suppressions(path: Path) -> list[Suppression]:
    suppressions: list[Suppression] = []
    for row, where in _rows(path):
        member_id = _text(row, "member_id", where, optional=True)
        phone = _text(row, "phone", where, optional=True)
        if member_id is None and phone is None:
            raise _fail(where, "needs a member_id, a phone or both")
        suppressions.append(
            Suppression(
                member_id=member_id,
                phone=phone,
                added_at=parse_timestamp(row.get("added_at"), f"{where} added_at"),
                source=_choice(row, "source", SUPPRESSION_SOURCES, where),
                attempt_id=_text(row, "attempt_id", where, optional=True),
            )
        )
    return suppressions


def _clock(value: object, key: str, where: str) -> time:
    if not isinstance(value, str):
        raise _fail(where, f"{key} must be a string like 08:00")
    try:
        hours, minutes = value.split(":")
        return time(int(hours), int(minutes))
    except ValueError:
        raise _fail(where, f"{key} must look like 08:00, got {value!r}") from None


def _count(value: object, key: str, where: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _fail(where, f"{key} must be an integer of at least {minimum}")
    return value


def load_policy(path: Path | None) -> Policy:
    if path is None:
        return Policy()
    if not path.is_file():
        raise InputError(f"{path}: file not found")
    where = path.name
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise _fail(where, f"not valid JSON ({error.msg})") from None
    if not isinstance(raw, dict):
        raise _fail(where, "must be a JSON object")
    known = {
        "window_start", "window_end", "max_attempts_per_day", "max_attempts_per_7_days",
        "writeback_deadline_minutes", "suppression_deadline_minutes", "require_disclosure",
        "writeback_dispositions",
    }
    unknown = sorted(set(raw) - known)
    if unknown:
        raise _fail(where, f"unknown key(s): {', '.join(unknown)}")
    base = Policy()
    start = _clock(raw["window_start"], "window_start", where) if "window_start" in raw else base.window_start
    end = _clock(raw["window_end"], "window_end", where) if "window_end" in raw else base.window_end
    if not start < end:
        raise _fail(where, "window_start must be earlier than window_end")
    require = raw.get("require_disclosure", base.require_disclosure)
    if not isinstance(require, bool):
        raise _fail(where, "require_disclosure must be true or false")
    dispositions = raw.get("writeback_dispositions", list(base.writeback_dispositions))
    if not isinstance(dispositions, list) or any(d not in DISPOSITIONS for d in dispositions):
        raise _fail(where, f"writeback_dispositions must be a list drawn from {', '.join(DISPOSITIONS)}")
    return Policy(
        window_start=start,
        window_end=end,
        max_attempts_per_day=_count(raw.get("max_attempts_per_day", base.max_attempts_per_day), "max_attempts_per_day", where, minimum=1),
        max_attempts_per_7_days=_count(raw.get("max_attempts_per_7_days", base.max_attempts_per_7_days), "max_attempts_per_7_days", where, minimum=1),
        writeback_deadline_minutes=_count(raw.get("writeback_deadline_minutes", base.writeback_deadline_minutes), "writeback_deadline_minutes", where, minimum=0),
        suppression_deadline_minutes=_count(raw.get("suppression_deadline_minutes", base.suppression_deadline_minutes), "suppression_deadline_minutes", where, minimum=0),
        require_disclosure=require,
        writeback_dispositions=tuple(dispositions),
    )


def load_ledger(directory: Path) -> tuple[Ledger, Policy]:
    """Read attempts.jsonl, writebacks.jsonl, suppressions.jsonl and, if present, policy.json."""
    policy_path = directory / "policy.json"
    ledger = Ledger(
        attempts=load_attempts(directory / "attempts.jsonl"),
        writebacks=load_writebacks(directory / "writebacks.jsonl"),
        suppressions=load_suppressions(directory / "suppressions.jsonl"),
    )
    return ledger, load_policy(policy_path if policy_path.is_file() else None)
