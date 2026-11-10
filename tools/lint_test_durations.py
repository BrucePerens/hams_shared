#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Measured run time of each hams_shared/tools test file, and the balanced split of those files into the
`pytest tools/[NofM]` lint steps.

The four chunks used to be sized by FILE LENGTH, which does not predict run time: one chunk took 450-660 s and
another 195 s. `balanced_chunks` assigns files longest-first to the chunk with the least estimated time
(the LPT rule, within 4/3 of optimal), using seconds measured per file (`lint_test_durations.json`, beside this
file) and, for a file with no measurement (new file), the median seconds per byte of the measured ones times its size.
The split is deterministic for a given file list and table, so the step names, argv and cache keys are stable.

Refresh the table after a large change in the suites (a measurement of a loaded machine is fine: only the ratios matter):

    cd tools && python3 -m pytest -q -n 4 --dist loadfile --junitxml=/tmp/tools.xml test_*.py
    python3 lint_test_durations.py refresh /tmp/tools.xml

CLI: `lint_test_durations.py refresh JUNIT_XML` (writes the table), `lint_test_durations.py show [N]` (the split of
the current files into N chunks with estimated seconds).
"""

import glob
import json
import math
import os
import statistics
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(globals().get("__file__", "lint_test_durations.py")))
TABLE = os.path.join(HERE, "lint_test_durations.json")


def load_table(path=None):
    try:
        with open(path or TABLE, encoding="utf-8") as handle:
            data = json.load(handle)
        table = {name: float(seconds) for name, seconds in data.get("seconds", {}).items()}
        return {name: seconds for name, seconds in table.items() if math.isfinite(seconds) and seconds >= 0}
    except (OSError, ValueError, AttributeError):
        return {}


def estimate(files, table):
    """{file: estimated seconds}. A file missing from `table` gets the median measured seconds per byte times its size
    (1 s per 100 kB when nothing is measured at all)."""
    rates = []
    for path in files:
        seconds = table.get(os.path.basename(path))
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if seconds is not None and size:
            rates.append(seconds / size)
    rate = statistics.median(rates) if rates else 1.0 / 100_000
    out = {}
    for path in files:
        known = table.get(os.path.basename(path))
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        out[path] = known if known is not None else size * rate
    return out


def balanced_chunks(files, count, table=None, split=None, collect=None):
    """`count` lists of `files`, balanced by estimated run time (longest first into the lightest chunk). Each list is
    sorted; the empty chunks are dropped. Deterministic. A file named in `split` (default: the "split_tests" section of the
    table, `load_split`) is not one unit but several (`split_units`): its slow tests one by one, the rest of its tests together,
    all as pytest node ids, so the slow tests of one file run in different chunks (pytest-xdist's --dist loadfile keeps a file
    on one worker). `collect` lists a file's node ids (default `collect_node_ids`); a file it cannot list stays whole."""
    table = load_table() if table is None else table
    split = load_split() if split is None else split
    seconds = estimate(files, table)
    units = []
    for path in files:
        parts = split_units(path, split[os.path.basename(path)], collect or collect_node_ids) if os.path.basename(path) in split else None
        units.extend(parts or [((path,), seconds[path])])
    chunks = [[] for _ in range(max(1, count))]
    load = [0.0] * len(chunks)
    for args, cost in sorted(units, key=lambda unit: (-unit[1], unit[0])):
        lightest = min(range(len(chunks)), key=lambda i: (load[i], i))
        chunks[lightest].extend(args)
        load[lightest] += cost
    return [sorted(chunk) for chunk in chunks if chunk]


def load_split(path=None):
    """{test file name: {"tests": {"Class::test": seconds}, "rest_seconds": seconds}} from the table's "split_tests" section."""
    try:
        with open(path or TABLE, encoding="utf-8") as handle:
            entry = json.load(handle).get("split_tests", {})
    except (OSError, ValueError, AttributeError):
        return {}
    return {name: value for name, value in entry.items() if isinstance(value, dict)} if isinstance(entry, dict) else {}


def collect_node_ids(path):
    """The node ids pytest collects from the test file `path`, as `<path>::<rest>` (absolute path kept), or None when collection
    fails. Runs `pytest --collect-only -q` in the file's directory."""
    import subprocess
    try:
        out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", path],
                             cwd=os.path.dirname(path), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    ids = [path + "::" + line.split("::", 1)[1] for line in out.stdout.splitlines() if "::" in line and not line.startswith(" ")]
    return ids or None


# [@ANCHOR: lint_test_durations:split_units]
def split_units(path, entry, collect):
    """Units ((node id, ...), seconds) of one split file: each test named in `entry["tests"]` alone with its seconds, every other
    collected test of the file together (`entry["rest_seconds"]`, default 1 s). None when the file cannot be collected or a named
    test no longer exists (the caller keeps the whole file then: a split can never drop or invent a test)."""
    ids = collect(path)
    if not ids:
        return None
    named = {name: float(value) for name, value in (entry.get("tests") or {}).items() if isinstance(value, (int, float))}
    local = {node.split("::", 1)[1]: node for node in ids}
    if not named or any(name not in local for name in named):
        return None
    units = [((local[name],), named[name]) for name in sorted(named)]
    rest = tuple(node for name, node in sorted(local.items()) if name not in named)
    if rest:
        units.append((rest, float(entry.get("rest_seconds", 1.0))))
    return units


def daemon_shards(rel_dir, files, path=None, disabled=False):
    """`files` (the test files of the daemon test directory `rel_dir`, relative to the repository root) as a list of
    lists, one per pytest session: a directory listed under "daemon_shards" in the table is split into that many sessions
    balanced by the recorded seconds per file (a file not listed is estimated from its size, as for tools/); any other
    directory, or `disabled`, stays one session. Pure and deterministic, like `balanced_chunks`.

    hams_simulated_bots/tests took 540-585 s in one process (real aiortc connections, 25 tests of 10-50 s each, most of
    it waiting); its sessions are independent (every server binds port 0 except test_main/test_observer, which use
    different fixed ports) so they overlap."""
    try:
        with open(path or TABLE, encoding="utf-8") as handle:
            entry = json.load(handle).get("daemon_shards", {}).get(rel_dir.replace(os.sep, "/"))
    except (OSError, ValueError, AttributeError):
        entry = None
    if disabled or not isinstance(entry, dict) or len(files) < 2:
        return [list(files)]
    try:
        count = int(entry.get("count", 1))
    except (TypeError, ValueError):
        return [list(files)]
    if count < 2:
        return [list(files)]
    seconds = {name: float(value) for name, value in entry.get("seconds", {}).items()
               if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0}
    return balanced_chunks(files, min(count, len(files)), seconds, split={})


def refresh(junit_path, table_path=None):
    """Write the table from a pytest junit XML: the sum of the test durations of each test file."""
    totals = {}
    for case in ET.parse(junit_path).getroot().iter("testcase"):
        classname = case.get("classname", "")
        parts = classname.split(".")
        name = next((p for p in parts if p.startswith("test_")), None)
        if name is None:
            continue
        totals[name + ".py"] = totals.get(name + ".py", 0.0) + float(case.get("time", 0) or 0)
    try:  # keep the other sections ("daemon_shards") of the table
        with open(table_path or TABLE, encoding="utf-8") as handle:
            table = json.load(handle)
    except (OSError, ValueError):
        table = {}
    table["_comment"] = "Seconds per tools/test_*.py file (sum of its tests, measured with pytest -n 4 on the dev box); see lint_test_durations.py."
    table["seconds"] = {k: round(v, 2) for k, v in sorted(totals.items())}
    with open(table_path or TABLE, "w", encoding="utf-8") as handle:
        json.dump(table, handle, indent=1)
        handle.write("\n")
    return totals


def main(argv):
    command = argv[0] if argv else "show"
    if command == "refresh" and len(argv) > 1:
        totals = refresh(argv[1])
        print(f"{len(totals)} files, {sum(totals.values()):.0f} s in total")
        return 0
    if command == "show":
        count = int(argv[1]) if len(argv) > 1 else 4
        files = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
        table = load_table()
        seconds = estimate(files, table)
        for index, chunk in enumerate(balanced_chunks(files, count, table), 1):
            print(f"chunk {index}: {len(chunk)} files, ~{sum(seconds.get(f, 0.0) for f in chunk):.0f} s")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
