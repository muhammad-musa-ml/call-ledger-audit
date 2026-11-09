"""Rebuild results/summary.md from scratch.

    python scripts/reproduce.py           write results/summary.md
    python scripts/reproduce.py --check   compare a fresh run with the committed file; exit 1 if they differ

Twenty seeds. For each, one dataset with planted defects and one clean dataset, both written to
disk and read back through the loader, so the file formats are part of what is measured.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from call_ledger_audit.checks import RULES, run_checks  # noqa: E402
from call_ledger_audit.generate import PLANTED, generate, score, write_dataset  # noqa: E402
from call_ledger_audit.model import load_ledger  # noqa: E402

SEEDS = range(1, 21)
MEMBERS = 150
REPS = 3
OUT = ROOT / "results" / "summary.md"


def run_one(seed: int, plant: bool, scratch: Path):
    dataset = generate(seed, members=MEMBERS, reps=REPS, plant=plant)
    directory = scratch / f"{'planted' if plant else 'clean'}-{seed}"
    write_dataset(dataset, directory)
    ledger, policy = load_ledger(directory)
    return dataset, ledger, run_checks(ledger, policy)


def span(values) -> str:
    return f"{min(values)} to {max(values)}"


def render() -> str:
    planted = Counter()
    found = Counter()
    missed = Counter()
    extra = Counter()
    sizes = {"planted": [], "clean": []}
    per_seed_pairs = set()
    clean_findings = 0
    with tempfile.TemporaryDirectory() as raw:
        scratch = Path(raw)
        for seed in SEEDS:
            dataset, ledger, findings = run_one(seed, True, scratch)
            sizes["planted"].append((len(ledger.attempts), len(ledger.writebacks), len(ledger.suppressions)))
            lost, stray = score(findings, dataset.planted)
            for rule, _, _ in dataset.planted:
                planted[rule] += 1
            for rule, _ in lost:
                missed[rule] += 1
            for rule, _ in stray:
                extra[rule] += 1
            hit = len(dataset.planted) - len(lost)
            per_seed_pairs.add((hit, len(dataset.planted), len(stray)))
            for rule in RULES:
                found[rule] = planted[rule] - missed[rule]
            _, clean_ledger, clean = run_one(seed, False, scratch)
            sizes["clean"].append((len(clean_ledger.attempts), len(clean_ledger.writebacks), len(clean_ledger.suppressions)))
            clean_findings += len(clean)

    def dims(kind):
        rows = sizes[kind]
        return (
            f"attempts {span([r[0] for r in rows])}, write-backs {span([r[1] for r in rows])}, "
            f"suppression entries {span([r[2] for r in rows])}"
        )

    variants = sum(len(v) for v in PLANTED.values())
    lines = [
        "# Reproduction results",
        "",
        f"Command: `python scripts/reproduce.py`. Seeds {SEEDS.start} to {SEEDS.stop - 1}. Each dataset has {MEMBERS} ordinary members "
        f"and the boundary cases; the planted runs add {REPS} copies of each of {variants} defect variants "
        f"({variants * REPS} planted defects per dataset). Every dataset is written to disk and read back before it is checked.",
        "",
        f"## Planted defects: {len(SEEDS)} datasets",
        "",
        f"Rows per dataset, smallest to largest: {dims('planted')}.",
        "",
        "| rule | planted | reported on the planted attempt | missed | reported on an unplanted attempt |",
        "|---|---:|---:|---:|---:|",
    ]
    for rule in RULES:
        lines.append(f"| {rule} | {planted[rule]} | {found[rule]} | {missed[rule]} | {extra[rule]} |")
    lines.append(
        f"| all | {sum(planted.values())} | {sum(found.values())} | {sum(missed.values())} | {sum(extra.values())} |"
    )
    pairs = "; ".join(f"{hit} of {total} found with {stray} unplanted" for hit, total, stray in sorted(per_seed_pairs))
    lines += [
        "",
        f"Per dataset: {pairs}.",
        "",
        f"## Clean datasets: the same {len(SEEDS)} seeds, nothing planted",
        "",
        f"Rows per dataset, smallest to largest: {dims('clean')}.",
        "",
        f"Findings in total: {clean_findings}.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare with the committed file instead of writing it")
    args = parser.parse_args()
    text = render()
    if args.check:
        if not OUT.is_file():
            print(f"{OUT} does not exist", file=sys.stderr)
            return 1
        if OUT.read_text(encoding="utf-8") != text:
            print("results/summary.md differs from a fresh run; run scripts/reproduce.py and review the diff", file=sys.stderr)
            return 1
        print("results/summary.md matches a fresh run")
        return 0
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
