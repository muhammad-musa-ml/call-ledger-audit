"""Command line: check a campaign directory, trace one member, or write synthetic data.

Exit codes for check: 0 no findings, 1 at least one finding, 2 an input file is wrong.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from .checks import RULES, run_checks
from .generate import generate, write_dataset
from .model import InputError, load_ledger
from .trace import member_timeline


def _report(ledger, findings) -> dict:
    counts = Counter(f.rule for f in findings)
    return {
        "attempts": len(ledger.attempts),
        "writebacks": len(ledger.writebacks),
        "suppressions": len(ledger.suppressions),
        "counts": {rule: counts.get(rule, 0) for rule in RULES},
        "findings": [
            {"rule": f.rule, "attempt_id": f.attempt_id, "member_id": f.member_id, "detail": f.detail}
            for f in findings
        ],
    }


def _check(args) -> int:
    ledger, policy = load_ledger(Path(args.dir))
    findings = run_checks(ledger, policy)
    for f in findings:
        print(f"{f.rule:<22} {f.attempt_id:<10} {f.member_id or '-':<8} {f.detail}")
    report = _report(ledger, findings)
    print(
        f"{report['attempts']} attempts, {report['writebacks']} write-backs, "
        f"{report['suppressions']} suppression entries: {len(findings)} finding(s)"
    )
    if findings:
        print("by rule: " + ", ".join(f"{rule} {n}" for rule, n in report["counts"].items() if n))
    if args.json:
        try:
            Path(args.json).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        except OSError as error:
            raise InputError(f"{args.json}: {error.strerror or error}") from None
    return 1 if findings else 0


def _trace(args) -> int:
    ledger, policy = load_ledger(Path(args.dir))
    findings = run_checks(ledger, policy)
    for line in member_timeline(ledger, args.member, findings):
        print(line)
    return 0


def _generate(args) -> int:
    try:
        dataset = generate(args.seed, members=args.members, reps=args.reps, plant=not args.no_plant)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    write_dataset(dataset, Path(args.out))
    ledger = dataset.ledger
    print(
        f"wrote {len(ledger.attempts)} attempts, {len(ledger.writebacks)} write-backs, "
        f"{len(ledger.suppressions)} suppression entries to {args.out}; "
        f"{len(dataset.planted)} planted defects listed in planted.json"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="call-ledger-audit", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="run every check over a directory of exports")
    check.add_argument("--dir", required=True, help="holds attempts.jsonl, writebacks.jsonl, suppressions.jsonl and optionally policy.json")
    check.add_argument("--json", help="also write the report to this file")
    check.set_defaults(run=_check)

    trace = commands.add_parser("trace", help="print one member's attempts, write-backs, suppressions and findings in time order")
    trace.add_argument("--dir", required=True)
    trace.add_argument("--member", required=True)
    trace.set_defaults(run=_trace)

    gen = commands.add_parser("generate", help="write a seeded synthetic dataset, with planted defects unless --no-plant")
    gen.add_argument("--seed", type=int, required=True)
    gen.add_argument("--out", required=True, help="directory to write; created if missing")
    gen.add_argument("--members", type=int, default=150, help="ordinary members (default 150)")
    gen.add_argument("--reps", type=int, default=3, help="times each of the 25 defect variants is planted (default 3)")
    gen.add_argument("--no-plant", action="store_true", help="clean traffic and boundary cases only")
    gen.set_defaults(run=_generate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except InputError as error:
        print(f"input error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
