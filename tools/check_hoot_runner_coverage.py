#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Hoot Runner Coverage Linter
----------------------------
A module can register a *.test.js file in its __manifest__.py's
"web.assets_unit_tests" bundle -- making it syntactically valid, bundled,
and loadable by hoot -- without anything in the Python test suite ever
actually executing it via browser_js(). That gap let real, broken hoot
suites (a whole class failing on a read-only `window.fetch` reassignment,
found 2026-09-09) sit unnoticed for a long time: nothing in CI ever ran
them, so nothing ever reported red. This check closes the gap at the
opposite end from check_test_tags.py -- it doesn't check that existing
test classes are tagged correctly, it checks that a hoot suite REGISTERED
in a manifest has at least one Python test file that actually runs it.

The check is module-level, not per-file: it flags a module that has ANY
*.test.js file in web.assets_unit_tests but NO tests/test_*.py file
containing a browser_js() call with a "[HOOT]" success_signal anywhere in
the module. It does not verify per-suite tag coverage (a module with ten
hoot test files and one runner that only covers one tag currently passes)
-- that finer-grained gap is real but needs a JS-side tag cross-reference
this check doesn't attempt.

Second check, added 2026-09-15: the SAME failure in the other direction.
A *.test.js file can exist on disk, with a registered Python wrapper
passing a matching tag=, and simply never be listed in any manifest
bundle. /web/tests only loads what a manifest's web.assets_unit_tests
names, so the wrapper's tag matches zero tests -- and Odoo's hoot runner
reports a run with no tests as "no tests to run", then "Passed 0 tests",
then "[HOOT] Test suite succeeded". That is character-for-character what
browser_js() waits for, so the wrapper passes. A green suite that never
executed an assertion is worse than a missing one, because it actively
reports coverage that does not exist.

That is not hypothetical here. A repo-wide scan on 2026-09-15 found six
such files: five in ham_shack, each with its own registered wrapper, and
theme_hams/static/tests/s_ham_map.test.js, whose four tests covered a
screen-reader accessibility fallback and had never once run. ham_shack's
own manifest already carried several comments warning about this exact
trap, added one file at a time as each was discovered by hand -- which is
the argument for checking it mechanically instead.

Fourth check, added 2026-09-18: the reverse of check_bundled_tag_is_triggered.
That one catches a declared tag nothing requests; this one catches a
REQUESTED tag nothing declares -- a browser_js() wrapper asking /web/tests
for a tag= that no describe.current.tags(...) anywhere writes. hoot reports
the resulting empty run identically to a genuine pass, so a typo in a
wrapper's own tag= (the realistic failure -- see
check_wrapper_requests_declared_tag's own docstring) passes forever while
the suite it meant to run never runs at all. Found 2026-09-15 while landing
the empty-run guard (zero_sudo/tests/test_hoot_empty_run_guard.py, hams_open
cdc3dfc1): that guard's own test deliberately requests a nonexistent tag,
and this checker stayed clean at the time -- there was no reverse check to
catch it.
"""

import ast
import os
import re
import sys
from urllib.parse import unquote

# A hoot suite declares which tag triggers it with `describe.current.tags("name")`, and a Python
# wrapper triggers it by asking /web/tests for `?tag=name`. Both are matched textually, which is
# what makes this check cheap; see check_bundled_tag_is_triggered's own docstring for the limits
# that buys.
_TAG_DECLARATION_RE = re.compile(r'describe\.current\.tags\(\s*["\']([^"\']+)["\']')
_TAG_REQUEST_RE = re.compile(r"[?&]tag=([A-Za-z0-9_.+%-]+)")
# `test(` but not `.test(` or `mytest(` -- hoot's own test() call, not a substring of something else.
_TEST_CALL_RE = re.compile(r"(?<![\w.])test\s*\(")
# check_wrapper_requests_declared_tag's own documented, deliberate exception: a wrapper
# whose tag= genuinely matches nothing on purpose (proving the empty-run guard fires, for
# example) carries this comment, with a real reason, adjacent to the browser_js() call.
_UNDECLARED_TAG_EXEMPTION_RE = re.compile(r"#\s*hoot-tag-intentionally-undeclared:\s*(\S.*)")

_IGNORE_DIRS = {
    ".git",
    "node_modules",
    "venv",
    "env",
    ".venv",
    "__pycache__",
    ".agents",
    "agents",
    "target",
    "radae",
    "site-packages",
    ".claude",
}


def find_hoot_test_files(manifest_dict):
    assets = manifest_dict.get("assets", {})
    unit_test_bundle = assets.get("web.assets_unit_tests", [])
    return [f for f in unit_test_bundle if isinstance(f, str) and f.endswith(".test.js")]


def find_unbundled_test_files(module_path, manifest_dict):
    """*.test.js files that exist under static/tests/ but appear in no manifest bundle.

    Checks every bundle, not just web.assets_unit_tests: a test file listed anywhere in the
    manifest is at least wired up on purpose, and deciding whether it sits in the RIGHT bundle
    needs the assets_unit_tests_setup reasoning that ham_shack's own manifest comments record
    at length. The failure this catches is cruder and unambiguous -- a file mentioned nowhere
    at all.
    """
    tests_dir = os.path.join(module_path, "static", "tests")
    if not os.path.isdir(tests_dir):
        return []

    listed = set()
    for bundle in (manifest_dict.get("assets") or {}).values():
        if not isinstance(bundle, (list, tuple)):
            continue
        for entry in bundle:
            if isinstance(entry, str):
                listed.add(os.path.basename(entry))

    unbundled = []
    for root, dirs, files in os.walk(tests_dir):
        # tours/ live in web.assets_tests and are driven by start_tour, not by a hoot tag.
        dirs[:] = [d for d in dirs if d not in ("tours", "__pycache__")]
        for f in files:
            if f.endswith(".test.js") and f not in listed:
                unbundled.append(os.path.relpath(os.path.join(root, f), module_path))
    return sorted(unbundled)


def _split_requested_tags(raw_match):
    """A single `tag=` value can request several suites at once, joined by a literal `+` (hoot's
    own multi-tag separator), which may itself be percent-encoded as `%2B` inside a Python string
    literal. Decoding first, then splitting, matters: splitting on a bare `+` before decoding
    would treat an encoded `%2B` as an ordinary tag character instead of a separator and silently
    merge two real tags into one bogus one, while decoding after collecting individual matches
    (rather than before) would be too late to split on the `+` it produces.
    """
    return [t for t in unquote(raw_match).split("+") if t]


def collect_requested_tags(repo_root):
    """Every hoot tag any browser_js() wrapper under `repo_root` asks /web/tests for.

    Collected across ALL scanned roots before any module is judged, because a wrapper and the
    suite it triggers need not be in the same module -- and an untagged `/web/tests` run would
    trigger everything, so it is reported separately rather than silently treated as coverage.
    """
    requested = set()
    untagged_runners = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
        if os.path.basename(root) != "tests":
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(root, f)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            if "browser_js(" not in content:
                continue
            for raw in _TAG_REQUEST_RE.findall(content):
                requested.update(_split_requested_tags(raw))
            for url in re.findall(r"""["'](/web/tests[^"']*)["']""", content):
                if "tag=" not in url:
                    untagged_runners.append(path)
    return requested, untagged_runners


def collect_declared_tags(repo_root):
    """Every hoot tag any describe.current.tags(...) call declares, across every *.test.js file
    under `repo_root`.

    Collected the same way `collect_requested_tags` collects its side of the cross-reference --
    across ALL scanned roots before any wrapper is judged, since a suite and the wrapper that
    triggers it need not live in the same repository.
    """
    declared = set()
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
        for f in files:
            if not f.endswith(".test.js"):
                continue
            path = os.path.join(root, f)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            declared.update(_TAG_DECLARATION_RE.findall(content))
    return declared


def check_bundled_tag_is_triggered(module_path, manifest_dict, requested_tags):
    """Bundled, loadable, declares a tag -- and nothing ever asks for that tag.

    The third face of the same silent pass, and the one this file's own docstring used to name as
    not attempted: "a module with ten hoot test files and one runner that only covers one tag
    currently passes -- that finer-grained gap is real but needs a JS-side tag cross-reference
    this check doesn't attempt." This is that cross-reference. The module-level check above is
    satisfied by a single runner, so a module can pass it while most of its suites never run.

    Also flags a bundled file that declares a tag but contains no `test()` call at all: hoot
    reports that identically to an empty run ("Passed 0 tests", then "Test suite succeeded"), so
    a wrapper asking for that tag goes green having asserted nothing.

    Textual matching, deliberately, which bounds what it can see: a tag built at runtime rather
    than written as a literal, or a suite whose tests are all skipped at runtime, are both
    invisible here. It finds the common, checkable case rather than attempting the general one.

    No baseline accompanies this check, and that is a finding rather than an omission. A scan on
    2026-09-15 found 18 bundled-but-untriggered tags across both repositories, and 17 of them are
    in the 13 modules that already carry `# burn-ignore-hoot-runner-coverage` and have no hoot
    runner of any kind -- the backlog `night_shift_todo/medium/hoot-runner-coverage-13-modules-
    d5b7c88b.md` already tracks. Because this check runs after that skip, it never sees them, so
    there is nothing to grandfather. The single remaining case was
    `user_websites/static/tests/violation_report.test.js`: a module that HAS a runner, and
    therefore passes the module-level check above, while three of its tests are triggered by
    nothing. That is precisely the shape this cross-reference exists to catch and the older check
    structurally cannot.
    """
    tests_dir = os.path.join(module_path, "static", "tests")
    if not os.path.isdir(tests_dir):
        return []

    listed = set()
    for bundle in (manifest_dict.get("assets") or {}).values():
        if isinstance(bundle, (list, tuple)):
            for entry in bundle:
                if isinstance(entry, str):
                    listed.add(os.path.basename(entry))

    violations = []
    for root, dirs, files in os.walk(tests_dir):
        dirs[:] = [d for d in dirs if d not in ("tours", "__pycache__")]
        for f in sorted(files):
            if not f.endswith(".test.js") or f not in listed:
                continue
            try:
                with open(os.path.join(root, f), "r", encoding="utf-8") as fh:
                    content = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            tags = _TAG_DECLARATION_RE.findall(content)
            if not tags:
                continue
            if not _TEST_CALL_RE.search(content):
                violations.append(
                    f"🚨 EMPTY HOOT SUITE: {os.path.basename(module_path)}/{f} declares tag(s) "
                    f"{', '.join(tags)} but contains no test() call. Hoot reports that exactly "
                    f"like an empty run -- \"Passed 0 tests\", then \"Test suite succeeded\" -- "
                    f"so any wrapper asking for it goes green having asserted nothing."
                )
                continue
            untriggered = [t for t in tags if t not in requested_tags]
            if untriggered:
                violations.append(
                    f"🚨 UNTRIGGERED HOOT SUITE: {os.path.basename(module_path)}/{f} declares "
                    f"tag(s) {', '.join(untriggered)} that no browser_js() wrapper anywhere asks "
                    f"/web/tests for, so these tests never run. Add a wrapper following "
                    f"theme_hams/tests/test_s_ham_map_hoot.py's pattern, or remove the tag if the "
                    f"suite is genuinely obsolete."
                )
    return violations


def check_wrapper_requests_declared_tag(repo_root, declared_tags):
    """A browser_js() wrapper asking /web/tests for a tag= that no describe.current.tags(...)
    anywhere declares.

    The reverse of check_bundled_tag_is_triggered: that one catches a declared tag nothing
    requests, this one catches a REQUESTED tag nothing declares. The realistic failure is a typo
    in a wrapper's own tag= -- hoot reports the resulting empty run identically to a genuine pass
    ("Passed 0 tests", then "Test suite succeeded"), so the wrapper goes green forever while the
    suite it meant to run never runs at all.

    One deliberate exception exists: zero_sudo/tests/test_hoot_empty_run_guard.py must request a
    tag that matches nothing, to prove HamsHttpCase.browser_js()'s own empty-run guard actually
    fires. So this cannot simply flag every undeclared tag -- it needs a single, tested,
    centralized exception rather than a loosened rule or a scattered ignore. A wrapper's file is
    exempted only when it carries BOTH `expect_empty=True` on a browser_js() call AND a
    `# hoot-tag-intentionally-undeclared: <reason>` comment with a real reason; carrying only one
    of the two is itself suspicious (an undocumented deliberate empty run, or a documented one
    that HamsHttpCase's own guard would still fail) and reported as its own finding rather than
    silently passed or silently exempted.

    File-level, not per-call: like every other check in this module, this matches textually
    across a whole file's content rather than parsing which browser_js() call a given tag= or
    expect_empty= belongs to. A file with several browser_js() calls, only one of which is the
    deliberate empty-run case, would need its own exemption comment to read clearly as covering
    that specific call, but would not be mis-flagged by this simplification -- it would just be
    exempted a little more broadly than strictly necessary, matching this file's own established
    "finds the common, checkable case rather than attempting the general one" philosophy.
    """
    violations = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
        if os.path.basename(root) != "tests":
            continue
        for f in sorted(files):
            if not f.endswith(".py"):
                continue
            path = os.path.join(root, f)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            if "browser_js(" not in content:
                continue

            requested_here = set()
            for raw in _TAG_REQUEST_RE.findall(content):
                requested_here.update(_split_requested_tags(raw))
            undeclared = sorted(t for t in requested_here if t not in declared_tags)
            if not undeclared:
                continue

            has_expect_empty = "expect_empty=True" in content
            exemption_match = _UNDECLARED_TAG_EXEMPTION_RE.search(content)
            has_reason = bool(exemption_match and exemption_match.group(1).strip())

            if has_expect_empty and has_reason:
                continue  # documented, deliberate -- the empty-run guard's own shape.

            rel_path = os.path.relpath(path, repo_root)
            if has_expect_empty and not has_reason:
                violations.append(
                    f"🚨 UNDOCUMENTED EMPTY HOOT RUN: {rel_path} passes expect_empty=True but "
                    f"has no `# hoot-tag-intentionally-undeclared: <reason>` comment explaining "
                    f"why -- a bare expect_empty=True is itself suspicious without a reason on "
                    f"record. Requested tag(s) with no matching describe(): "
                    f"{', '.join(undeclared)}."
                )
            elif has_reason and not has_expect_empty:
                violations.append(
                    f"🚨 UNDECLARED HOOT TAG WITHOUT expect_empty: {rel_path} carries a "
                    f"`# hoot-tag-intentionally-undeclared:` comment but no browser_js() call "
                    f"there passes expect_empty=True, so an empty run still fails "
                    f"HamsHttpCase's own empty-run guard. Requested tag(s) with no matching "
                    f"describe(): {', '.join(undeclared)}."
                )
            else:
                violations.append(
                    f"🚨 REQUESTED HOOT TAG NEVER DECLARED: {rel_path} asks /web/tests for "
                    f"tag(s) {', '.join(undeclared)} that no describe.current.tags(...) "
                    f"anywhere declares -- a typo, or a suite that was renamed or removed. "
                    f"hoot reports the resulting empty run identically to a real pass, so this "
                    f"wrapper passes forever without ever running the suite it meant to. If "
                    f"this is genuinely deliberate (proving an empty-run guard fires, for "
                    f"example), pass expect_empty=True on the browser_js() call AND add a "
                    f"`# hoot-tag-intentionally-undeclared: <reason>` comment explaining why, "
                    f"following zero_sudo/tests/test_hoot_empty_run_guard.py's own pattern."
                )
    return violations


def module_has_hoot_runner(module_path):
    tests_dir = os.path.join(module_path, "tests")
    if not os.path.isdir(tests_dir):
        return False
    for root, dirs, files in os.walk(tests_dir):
        for f in files:
            if not f.endswith(".py"):
                continue
            filepath = os.path.join(root, f)
            try:
                with open(filepath, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            if "browser_js(" in content and "[HOOT]" in content:
                return True
    return False


def main():
    if len(sys.argv) < 2:
        print("Usage: check_hoot_runner_coverage.py <repository_root> [more_roots...]")
        sys.exit(1)

    violations = []

    # Pass 1: every tag any wrapper asks for, and every tag any suite declares, across every
    # scanned root, before judging any module or wrapper -- neither side of either cross-
    # reference needs to live in the same module, or even the same repository, as its match.
    requested_tags = set()
    declared_tags = set()
    untagged_runners = []
    for repo_root in sys.argv[1:]:
        found, untagged = collect_requested_tags(repo_root)
        requested_tags |= found
        untagged_runners.extend(untagged)
        declared_tags |= collect_declared_tags(repo_root)

    # An untagged /web/tests run triggers every bundled suite, so while one exists, "no wrapper
    # asks for this tag" is not evidence that a suite never runs. Report it rather than quietly
    # suppressing the check or quietly ignoring it.
    if untagged_runners:
        print(
            "[*] Untriggered-tag check skipped: these wrappers run /web/tests with no tag=, "
            "which triggers every bundled suite: " + ", ".join(sorted(set(untagged_runners)))
        )

    # A wrapper requesting a tag no suite declares is judged per-repo-root (like Pass 2's
    # module-level checks below), not once globally, since its own violation message reports a
    # path relative to the root it was found under.
    for repo_root in sys.argv[1:]:
        violations.extend(check_wrapper_requests_declared_tag(repo_root, declared_tags))

    # Pass 2: judge each module.
    for repo_root in sys.argv[1:]:
        for root, dirs, files in os.walk(repo_root):
            dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
            if "__manifest__.py" not in files:
                continue

            manifest_path = os.path.join(root, "__manifest__.py")
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest_source = f.read()
                    tree = ast.parse(manifest_source, filename=manifest_path)
            except (OSError, SyntaxError):
                continue

            # Guardrail Preservation Mandate, matching check_test_tags.py's
            # own precedent: an explicit, reviewed exception for a module
            # that's already known to be missing a runner (tracked as a
            # real follow-up, not silently swept under the rug) shouldn't
            # be a hard failure every time run_linters.py runs.
            if "# burn-ignore-hoot-runner-coverage" in manifest_source:
                continue

            manifest_dict = None
            for node in tree.body:
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Dict):
                    try:
                        manifest_dict = ast.literal_eval(node.value)
                    except ValueError:
                        continue
                    break
            if manifest_dict is None:
                continue

            mod_name_for_unbundled = os.path.basename(root)
            unbundled = find_unbundled_test_files(root, manifest_dict)
            if unbundled:
                violations.append(
                    f"🚨 UNBUNDLED HOOT SUITE: module '{mod_name_for_unbundled}' has "
                    f"{len(unbundled)} *.test.js file(s) on disk that no bundle in its "
                    f"__manifest__.py lists ({', '.join(unbundled)}). /web/tests only loads "
                    f"what a manifest names, so any wrapper's tag= matches zero tests -- and "
                    f"hoot reports an empty run as \"Test suite succeeded\", so that wrapper "
                    f"PASSES while executing no assertions at all. Add each file (and any "
                    f"source JS it imports that isn't already reachable) to "
                    f"web.assets_unit_tests, following ham_shack/__manifest__.py's own "
                    f"precedent, or delete the file if it is genuinely obsolete."
                )

            if not untagged_runners:
                violations.extend(
                    check_bundled_tag_is_triggered(root, manifest_dict, requested_tags)
                )

            hoot_test_files = find_hoot_test_files(manifest_dict)
            if not hoot_test_files:
                continue

            if not module_has_hoot_runner(root):
                mod_name = os.path.basename(root)
                violations.append(
                    f"🚨 HOOT RUNNER COVERAGE VIOLATION: module '{mod_name}' registers "
                    f"{len(hoot_test_files)} *.test.js file(s) in web.assets_unit_tests "
                    f"({', '.join(hoot_test_files)}) but has no tests/test_*.py file that "
                    f"calls browser_js(...) with a \"[HOOT]\" success_signal -- these hoot "
                    f"suites are never actually executed by the Python test suite. Add a "
                    f"runner following ham_dx_cluster/tests/test_dx_cluster_widget_hoot.py's "
                    f"pattern."
                )

    if violations:
        for v in violations:
            print(v)
        sys.exit(1)

    print(
        "[+] Hoot Runner Coverage Linter: every *.test.js on disk is bundled, every bundled "
        "suite's tag is asked for by a real wrapper, every wrapper's requested tag is declared "
        "by a real suite, and every module with hoot unit tests has a runner."
    )


if __name__ == "__main__":
    main()
