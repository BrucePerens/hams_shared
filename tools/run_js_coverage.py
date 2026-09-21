#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
JS coverage bundle-offset mapping (ANCHOR_COVERAGE_AND_REMEDIATION_PLAN.md Stage 2's JS sub-track)

This is the piece of the JS sub-track that turns a real, live-collected V8 coverage report --
keyed by bundle URL and byte offset within that one concatenated file -- back into real
repo-relative source file paths and line numbers, the same output shape the Rust sub-track's
`run_rust_coverage.py` already produces for Stage 3's own cross-reference against anchor keys:
`{"files": {relpath: {"executed_lines": [...], "missing_lines": [...]}}}`.

**Real, verified finding this is built on, not assumed**: a real tour test (`ham_dns`'s
`TestDNSTour.test_01_bdd_test_dns_tour`) run under `/my/dns?debug=assets` instead of the normal
`debug=1` was confirmed, live, to compose cleanly with `start_tour()` (the tour passes identically),
and the resulting debug-mode bundle body (fetched directly via `self.url_open(...)` in that same
run) was confirmed to carry an explicit, structured separator comment before every concatenated
source module:

    /*********************************************
    *  Filepath: /web/static/lib/luxon/luxon.js  *
    *  Lines: 8606                               *
    *********************************************/

-- a real, repo-relative-ish path (`/web/static/lib/luxon/luxon.js`, an Odoo addon-relative static
path, not a `hams_open`/`hams_com`-repo-relative one -- see `resolve_addon_static_path()` below for
the real path-shape reconciliation this requires) and the exact line count of the module that
follows, repeated for every module Odoo's own asset bundler concatenated. This is what
`parse_bundle_module_offsets()` below parses; it needs no source-map library and no heuristics,
since the format is fixed and already emitted by Odoo core itself.

**Live collection** lives in `zero_sudo/tests/common.py` (`_patched_chrome_init` /
`_patched_chrome_stop`), opt-in via `HAMS_JS_COVERAGE_DIR=<dir>`: when set, every tour/browser
Chrome enables `Profiler.startPreciseCoverage` (call counts, detailed block ranges) right after it
starts, and takes the result just before Chrome stops, writing one JSON record per browser
(`{"test": id, "scripts": [{"url", "functions"}], "bundles": {url: body_text}}`) into that
directory. Run the tour under `debug=assets` (`HAMS_TOUR_TOUR_DEBUG=assets`) so the bundle bodies
carry the module headers this script parses. `map_coverage_records()` below then turns those
records into `{"files": {relpath: {"executed_lines": [...], "missing_lines": [...]}}}`.
Line granularity is V8 block coverage sampled at each line's first non-blank character, so a
comment or closing-brace line inside an executed range counts as executed.
"""

import argparse
import bisect
import json
import os
import re
import sys

_MODULE_HEADER_RE = re.compile(
    r"/\*{5,}[ \t]*\n"
    r"\*[ \t]*Filepath:[ \t]*(?P<filepath>\S+)[ \t]*\*[ \t]*\n"
    r"\*[ \t]*Lines:[ \t]*(?P<lines>\d+)[ \t]*\*[ \t]*\n"
    r"\*{5,}/[ \t]*\n"
)


def parse_bundle_module_offsets(bundle_text):
    """Returns a list of `(start_line, end_line, filepath)` tuples, one per module, in the order
    they appear in `bundle_text` -- `start_line`/`end_line` are 1-indexed, inclusive line numbers
    within the bundle (matching the 1-indexed convention V8's own `Profiler.takePreciseCoverage`
    uses for `startOffset`/`endOffset` once converted from byte offsets to line numbers by the
    caller), and `filepath` is exactly the string Odoo's own bundler wrote into the header --
    an Odoo addon-relative static path (e.g. `/web/static/lib/luxon/luxon.js`), not yet resolved to
    a real filesystem/repo-relative path. Each module's own header comment itself is not part of
    its reported line range -- the header's line count is the real module body only, matching the
    module's own file's real line count, so a `[@ANCHOR: ...]` line number found in the original
    source file lines up directly with an offset inside this module's own range.
    """
    modules = []
    for match in _MODULE_HEADER_RE.finditer(bundle_text):
        # 1-indexed line number where this header's own "/***...***" line starts, computed fresh
        # from an absolute position each time (not accumulated across iterations) so an error in
        # one module's own line count can never silently throw off every module after it.
        header_start_line = bundle_text.count("\n", 0, match.start()) + 1
        body_start_line = header_start_line + match.group(0).count("\n")
        line_count = int(match.group("lines"))
        body_end_line = body_start_line + line_count - 1
        modules.append((body_start_line, body_end_line, match.group("filepath")))
    return modules


def resolve_addon_static_path(addon_relative_path, addons_path_dirs):
    """Resolves an Odoo addon-relative static path (e.g. `/web/static/lib/luxon/luxon.js`, the
    exact string Odoo's own bundler writes into each module's `Filepath:` header) to a real,
    on-disk, repo-relative path, by checking each real addons-path directory in turn for a
    matching file -- the same "search every addons-path root" resolution Odoo's own module loader
    does, not assumed to be any single fixed prefix. Returns `None` if no addons-path directory has
    a matching file (a real gap to report, not silently swallow) rather than guessing.
    """
    parts = addon_relative_path.lstrip("/").split("/", 1)
    if len(parts) != 2:
        return None
    module_name, rest = parts
    for addons_dir in addons_path_dirs:
        candidate = os.path.join(addons_dir, module_name, rest)
        if os.path.isfile(candidate):
            return candidate
    return None


def _utf16_len(text):
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def line_hits_for_script(text, functions):
    """Turns one script's V8 `functions` coverage (each with `ranges` of `startOffset`/`endOffset`
    in UTF-16 code units and a `count`) into `{line_number: count}` for every non-blank line of
    `text` covered by at least one range. Ranges are applied outer to inner (start ascending, end
    descending) so a nested block's own count overrides its enclosing function's, as V8 specifies.
    A line takes the count of the innermost range containing its first non-blank character."""
    line_numbers = []
    first_offsets = []
    offset = 0
    for number, line in enumerate(text.split("\n"), start=1):
        stripped = line.lstrip()
        if stripped:
            line_numbers.append(number)
            first_offsets.append(offset + _utf16_len(line[: len(line) - len(stripped)]))
        offset += _utf16_len(line) + 1
    counts = [None] * len(line_numbers)
    ranges = [r for fn in functions for r in fn.get("ranges", [])]
    ranges.sort(key=lambda r: (r["startOffset"], -r["endOffset"]))
    for r in ranges:
        lo = bisect.bisect_left(first_offsets, r["startOffset"])
        hi = bisect.bisect_left(first_offsets, r["endOffset"])
        for i in range(lo, hi):
            counts[i] = r["count"]
    return {n: c for n, c in zip(line_numbers, counts) if c is not None}


def merge_file_reports(reports):
    """Unions several `{relpath: {"executed_lines", "missing_lines"}}` maps: a line executed in
    any report is executed, and is removed from `missing_lines`."""
    executed = {}
    seen = {}
    for report in reports:
        for path, entry in report.items():
            executed.setdefault(path, set()).update(entry["executed_lines"])
            seen.setdefault(path, set()).update(entry["executed_lines"], entry["missing_lines"])
    return {
        path: {
            "executed_lines": sorted(executed[path]),
            "missing_lines": sorted(seen[path] - executed[path]),
        }
        for path in sorted(seen)
    }


def map_coverage_record(record, addons_path_dirs, repo_root):
    """Maps one collected record (see the module docstring) to `{relpath: {"executed_lines",
    "missing_lines"}}`. Scripts with no bundle body, and modules whose addon path does not resolve
    to a file under an addons directory, are skipped (the second kind is listed under the
    returned second value, `unresolved`, so a gap is reported rather than hidden)."""
    files = {}
    unresolved = set()
    for script in record.get("scripts", []):
        text = record.get("bundles", {}).get(script["url"])
        if text is None:
            continue
        hits = line_hits_for_script(text, script["functions"])
        modules = parse_bundle_module_offsets(text)
        if not modules:
            # Minified bundle (no debug=assets, or debug lost on a redirect): no module headers,
            # so there is nothing to map. Reported, never silently dropped.
            unresolved.add(f"(no module headers: {script['url']})")
            continue
        for start, end, addon_path in modules:
            real = resolve_addon_static_path(addon_path, addons_path_dirs)
            if real is None:
                unresolved.add(addon_path)
                continue
            rel = os.path.relpath(real, repo_root)
            entry = files.setdefault(rel, {"executed": set(), "missing": set()})
            for line in range(start, end + 1):
                if line in hits:
                    bucket = "executed" if hits[line] > 0 else "missing"
                    entry[bucket].add(line - start + 1)
    report = {
        rel: {
            "executed_lines": sorted(e["executed"]),
            "missing_lines": sorted(e["missing"] - e["executed"]),
        }
        for rel, e in files.items()
    }
    return report, sorted(unresolved)


def map_coverage_records(records, addons_path_dirs, repo_root):
    """Maps and merges many records into the final `{"files": ...}` document plus the sorted list
    of unresolved addon paths."""
    reports = []
    unresolved = set()
    for record in records:
        report, missing = map_coverage_record(record, addons_path_dirs, repo_root)
        reports.append(report)
        unresolved.update(missing)
    return {"files": merge_file_reports(reports), "unresolved": sorted(unresolved)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "bundle_file",
        nargs="?",
        help="Path to a saved debug-mode asset bundle body (e.g. via curl/url_open); prints its module offsets",
    )
    parser.add_argument(
        "--coverage-dir",
        help="Directory of records written under HAMS_JS_COVERAGE_DIR; prints the mapped per-file report",
    )
    parser.add_argument(
        "--addons-path", default="", help="Comma-separated addons directories, for --coverage-dir"
    )
    parser.add_argument("--repo-root", default=os.getcwd(), help="Paths are reported relative to this")
    args = parser.parse_args()
    if args.coverage_dir:
        records = []
        for name in sorted(os.listdir(args.coverage_dir)):
            if name.endswith(".json"):
                with open(os.path.join(args.coverage_dir, name), "r", encoding="utf-8") as f:
                    records.append(json.load(f))
        addons = [d for d in args.addons_path.split(",") if d]
        print(json.dumps(map_coverage_records(records, addons, args.repo_root), indent=2))
        return 0
    if not args.bundle_file:
        parser.error("give a bundle_file or --coverage-dir")
    with open(args.bundle_file, "r", encoding="utf-8") as f:
        bundle_text = f.read()
    modules = parse_bundle_module_offsets(bundle_text)
    print(json.dumps([{"start_line": s, "end_line": e, "filepath": p} for s, e, p in modules], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
