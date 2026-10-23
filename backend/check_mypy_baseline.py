#!/usr/bin/env python3
"""Gate `mypy` output against a frozen baseline of pre-existing errors.

AUD-690 added mypy strict to CI, but the first real run was 88 errors across
33 files — too red to fix in the same change that wires up the gate (see
mypy-baseline.txt and the follow-up cleanup issue it was filed under). This
script lets CI fail only on errors that are NOT in the baseline, so new type
errors are caught from day one while the existing backlog is burned down
separately.

Usage:
    mypy src | python check_mypy_baseline.py mypy-baseline.txt

Matches by (file, error code, message) rather than by line number, so an
unrelated edit that shifts a line above a pre-existing error does not trip the
gate. Lines in the baseline that no longer appear in the current run are
treated as fixed and silently dropped — nothing re-adds them.
"""

import re
import sys
from collections import Counter

ERROR_RE = re.compile(r"^(?P<file>[^:]+):\d+: error: (?P<msg>.*?)(?: {2}\[(?P<code>[\w-]+)\])?$")


def normalize(line: str) -> str | None:
    m = ERROR_RE.match(line)
    if not m:
        return None
    return f"{m['file']}\t{m['code'] or ''}\t{m['msg']}"


def load(lines: list[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for line in lines:
        key = normalize(line.rstrip("\n"))
        if key is not None:
            counts[key] += 1
    return counts


def main() -> int:
    baseline_path = sys.argv[1]
    current_lines = sys.stdin.read().splitlines()

    with open(baseline_path) as f:
        baseline_counts = load(f.readlines())
    current_counts = load(current_lines)

    new_keys = current_counts - baseline_counts
    if not new_keys:
        print(f"mypy: {sum(current_counts.values())} pre-existing error(s) (baselined), 0 new.")
        return 0

    print("New mypy error(s) not covered by the baseline:\n", file=sys.stderr)
    remaining = Counter(new_keys)
    for line in current_lines:
        key = normalize(line.rstrip("\n"))
        if key is not None and remaining.get(key, 0) > 0:
            print(line, file=sys.stderr)
            remaining[key] -= 1
    print(
        f"\n{sum(new_keys.values())} new error(s). Fix them, or if this is a "
        f"deliberate baseline update, regenerate {baseline_path}.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
