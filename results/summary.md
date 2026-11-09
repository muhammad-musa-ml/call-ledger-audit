# Reproduction results

Command: `python scripts/reproduce.py`. Seeds 1 to 20. Each dataset has 150 ordinary members and the boundary cases; the planted runs add 3 copies of each of 25 defect variants (75 planted defects per dataset). Every dataset is written to disk and read back before it is checked.

Generated with Python 3.13. The standard random module does not promise the same draws across versions, so the dataset sizes below can differ on another Python; the table does not.

## Planted defects: 20 datasets

Rows per dataset, smallest to largest: attempts 388 to 425, write-backs 295 to 336, suppression entries 53 to 66.

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

Per dataset: 75 of 75 found with 0 unplanted.

## Clean datasets: the same 20 seeds, nothing planted

Rows per dataset, smallest to largest: attempts 292 to 329, write-backs 223 to 264, suppression entries 32 to 45.

Findings in total: 0.
