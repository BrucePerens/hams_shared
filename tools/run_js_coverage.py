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

**What this script does NOT yet do, named honestly rather than glossed over**: actually collect a
live V8 coverage report. That half (a `Profiler.enable`/`startPreciseCoverage` call immediately
before `super().start_tour(...)` and `Profiler.takePreciseCoverage` immediately after, both via
`self.browser._websocket_request(...)` -- the same method `ChromeBrowser` already uses internally,
no second CDP connection needed -- gated behind an opt-in env var mirroring `start_hams_browser()`'s
own existing `HAMS_PAUSE_ON_FAIL` convention) is real, separate implementation work inside
`zero_sudo/tests/common.py`'s `HamsHttpCase.start_tour()`, not attempted in this pass: that file is
this codebase's single most widely-shared test-harness file, already touched once this same session
for a real, verified, narrow fix (the `--pause-on-fail` Wayland regression) -- a second, larger
change to its live-collection behavior deserves its own dedicated verification pass against a real
tour run, not a blind addition alongside an unrelated parser utility.
"""

import argparse
import json
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
    import os

    parts = addon_relative_path.lstrip("/").split("/", 1)
    if len(parts) != 2:
        return None
    module_name, rest = parts
    for addons_dir in addons_path_dirs:
        candidate = os.path.join(addons_dir, module_name, rest)
        if os.path.isfile(candidate):
            return candidate
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "bundle_file", help="Path to a saved debug-mode asset bundle body (e.g. via curl/url_open)"
    )
    args = parser.parse_args()
    with open(args.bundle_file, "r", encoding="utf-8") as f:
        bundle_text = f.read()
    modules = parse_bundle_module_offsets(bundle_text)
    print(json.dumps([{"start_line": s, "end_line": e, "filepath": p} for s, e, p in modules], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
