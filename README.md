# call-ledger-audit

call-ledger-audit audits the records an outbound voice-agent campaign leaves behind: the call log, the outcomes the agent wrote back to the system of record, and the do-not-call and opt-out list. It has nine checks. Each one is a plain function over the three exports. The limits a check applies (the calling hours, the attempt caps, the deadlines, which dispositions need a write-back, whether a disclosure is required) come from a policy: a `policy.json`, or the defaults listed below when there is none. Two things are fixed in the code: the 7-day window is 7 x 24 hours, and the table of outcomes each disposition can produce. It needs Python 3.10 or newer, and `tzdata` on Windows.

**Every dataset in this repository is synthetic.** They come from a seeded generator I wrote. Nothing was exported from a real campaign, and the numbers below describe my generator, not any real calling program.

## Why

A campaign leaves three records, and most questions an auditor asks are joins across them. Was this member called outside the window in their own time zone? Did an opt-out reach the suppression list? Does every conversation have a write-back, and is it one that call could have produced? I wrote the joins as checks that can run every night, and a `trace` command that prints one member's whole history in time order, so the answer for one member is one printout and not three exports.

## Run it

From the repository folder, with the virtual environment activated the way your shell does it:

```
python -m venv .venv
python -m pip install -e ".[dev]"
call-ledger-audit generate --seed 1 --out run
call-ledger-audit check --dir run
call-ledger-audit trace --dir run --member m0195
```

`check` exits 0 when it finds nothing, 1 when it finds something and 2 when an input file is wrong, so it can gate a nightly job. It exits 1 on this dataset, because `generate` plants defects; add `--no-plant` to write only clean traffic. `trace` on the last line prints this:

```
member m0195, phone +10005550195
2026-11-04 11:12:39 CST  17:12:39Z  call a00352  opt_out, disclosure played
2026-11-04 11:17:39 CST  17:17:39Z  write-back for a00352  opt_out
2026-11-04 11:22:39 CST  17:22:39Z  suppression entry  opt_out_call
2026-11-06 11:12:39 CST  17:12:39Z  call a00353  connected, disclosure played
    ! suppressed: opt_out_call entry added 2026-11-04T17:22:39Z, attempt began 2026-11-06T17:12:39Z
2026-11-06 11:17:39 CST  17:17:39Z  write-back for a00353  declined
```

## Input files

`--dir` holds three JSON Lines files and an optional `policy.json`. Timestamps look like `2026-03-02T15:04:05Z` or `2026-03-02T09:04:05-06:00`; a time with no `Z` or offset is refused because every check compares instants. Phone numbers are compared after spaces, dashes, dots and brackets are removed, so `+1 (312) 555-0101` and `+13125550101` are one number; a value that is not 7 to 15 digits is refused. A leading byte-order mark is accepted. A file that is not UTF-8 (a PowerShell `>` redirect writes UTF-16) is refused with exit 2, not a traceback.

| file | one line per | fields |
|---|---|---|
| `attempts.jsonl` | dial attempt | `attempt_id`, `member_id`, `phone`, `tz` (an IANA name), `started_at`, `disposition` (`no_answer`, `voicemail`, `connected`, `opt_out`), `disclosure_played` |
| `writebacks.jsonl` | outcome written to the system of record | `attempt_id`, `written_at`, `outcome` (`promise_to_pay`, `callback_requested`, `declined`, `opt_out`, `no_contact`), `promise_amount`, `promise_date` |
| `suppressions.jsonl` | do-not-call or opt-out entry | `member_id` and/or `phone`, `added_at`, `source` (`dnc`, `opt_out_call`), optional `attempt_id` of the call that produced it |
| `policy.json` | (one object) | `window_start`, `window_end`, `max_attempts_per_day`, `max_attempts_per_7_days`, `writeback_deadline_minutes`, `suppression_deadline_minutes`, `require_disclosure`, `writeback_dispositions` |

The defaults are mine: a window of 08:00 to 21:00 in the member's local time, 2 attempts a day, 5 in any 7 days, a write-back within 30 minutes of the attempt's start (there is no end-of-call field), an opt-out on the suppression list within 60 minutes of the attempt's start, a disclosure required whenever someone spoke, and a write-back required for `connected` and `opt_out` attempts. The window takes its hours from 47 CFR 64.1200(c)(1), which bars telephone solicitations to residential subscribers "before the hour of 8 a.m. or after 9 p.m. (local time at the called party's location)". Two differences: the rule's text does not bar a call that begins at exactly 9 p.m., and this tool treats the end of the window as exclusive, so it is stricter by that instant; and the `tz` field of an attempt stands in for the called party's location. Whether that rule governs a client's calls is for their compliance team, so the window is a setting. This is not legal advice.

## The checks

| rule | reports an attempt when |
|---|---|
| `outside_window` | it began outside the window in the member's own time zone, daylight saving included |
| `suppressed` | it began at or after a suppression entry for the member or the phone, unless the entry names this call |
| `no_disclosure` | someone spoke (`connected` or `opt_out`) and `disclosure_played` is false |
| `attempt_cap` | it is past the daily cap (per local day) or the cap for the 7 x 24 hours ending at it |
| `writeback_deadline` | it needs a write-back and has none by the deadline, or a write-back is stamped before the call began |
| `orphan_writeback` | a write-back names an attempt that is not in the call log |
| `outcome_mismatch` | the write-back outcome is one the disposition cannot produce, or two write-backs disagree |
| `promise_incomplete` | a promise to pay has no amount, a zero or negative amount, or no date |
| `opt_out_not_suppressed` | the member opted out, on the call or in a write-back, and no suppression entry exists by the deadline |

The boundaries are in the tests: a call at exactly 08:00:00 passes and one at exactly 21:00:00 does not, an attempt exactly 7 days earlier does not count toward the weekly cap, and a write-back exactly at its deadline is on time.

## What the checks find

`python scripts/reproduce.py` writes `results/summary.md`. It builds 20 seeded datasets, each with 150 ordinary members, boundary calls that must stay clean (the first and last second of the window, both clock-change days, the last attempt the cap allows) and 3 copies of each of 25 defect variants, 75 planted defects. It writes each dataset to disk, reads it back, runs the checks and compares the findings with the plant labels. Then it runs the checks on 20 clean datasets built from the same seeds. With `--check` it compares a fresh run with the committed file, which a test also does.

| rule | planted | reported on the planted attempt | missed | reported on an unplanted attempt |
|---|---:|---:|---:|---:|
| outside_window | 300 | 300 | 0 | 0 |
| suppressed | 240 | 240 | 0 | 0 |
| no_disclosure | 120 | 120 | 0 | 0 |
| attempt_cap | 120 | 120 | 0 | 0 |
| writeback_deadline | 120 | 120 | 0 | 0 |
| orphan_writeback | 60 | 60 | 0 | 0 |
| outcome_mismatch | 240 | 240 | 0 | 0 |
| promise_incomplete | 180 | 180 | 0 | 0 |
| opt_out_not_suppressed | 120 | 120 | 0 | 0 |
| all | 1500 | 1500 | 0 | 0 |

How to read it. Each cell is a total over the 20 planted datasets. The last column counts findings whose rule and attempt id were not among the plants, so a finding of the wrong rule on a planted attempt would also land there. These counts say the checks agree with my generator: every defect I planted was reported on the attempt I planted it on, and nothing was reported on the clean datasets. They do not estimate how often real campaigns break these rules. `results/summary.md` records which Python made it; another Python version can change the dataset sizes, because the standard random module does not promise the same draws, but not the table.

The first 20-seed run was not this clean. In one dataset the generator drew a 0-second delay for an opt-out entry, so the entry was stamped at the call's own start and `suppressed` flagged the call that produced it. An export could stamp an opt-out that way; I have not seen a real one. An entry can now name its call with `attempt_id` and is then not held against it; an entry without that field is still held against a call that began at or after its time.

## Tests

```
python -m pytest
python scripts/mutate.py
```

scripts/mutate.py applies 49 hand-made changes to the checks, the loader and the commands (for example, `<` turned into `<=` at the end of the calling window), each in its own temporary copy of the repository, and requires the test suite to fail on every one. It prints one line per change and exits 1 if any change survives or cannot be run. Its last run printed `49 mutants: 49 killed, 0 survived, 0 unrun`.

## What it does not show

- It has not seen a real campaign. Every number above is on synthetic data whose defects I chose.
- It needs the exports mapped into these four files. I have not mapped any platform's export. The time zone is a field on each attempt; an export that carries only a phone number needs an area-code lookup first.
- It does not read what was said on a call. A separate repository of mine, call-trace-checks (https://github.com/muhammad-musa-ml/call-trace-checks), runs checks over a call's transcript and tool log.
- Voice only. It knows nothing about text-message channels.
- The default limits are my numbers, not any client's policy.

MIT license.
