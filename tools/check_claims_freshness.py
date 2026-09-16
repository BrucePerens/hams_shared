#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Function-Level Claims Freshness Check (ADR 0091)

A "claim," per ADR 0091, is a short markdown file at `<module>/claims/<anchor_name>.md`
documenting precisely and falsifiably what one anchored function guarantees -- the same
falsifiable-claim discipline that found real production bugs during the 2026-09-08 patent
re-verification passes (see `hams_shared/agents/skills/bug-hunt/SKILL.md`), applied to ordinary
functions rather than patent claims. A claim file's own YAML frontmatter records `anchor` (the
base anchor name the claim documents, matching a real `# [@ANCHOR: name]` in source) and
`code_hash` (a sha256 of the anchored function's own source span, as it existed the last time a
human or AI actually confirmed the claim's own prose still matches the code).

This script does not, and cannot, verify that a claim's *prose* is still true -- that needs a
reader, human or AI, actually comparing the two. What it verifies mechanically, cheaply, and
without any semantic understanding at all: whether the anchored function's *source text* has
changed since the claim was last confirmed. A hash mismatch does not mean the claim is wrong -- it
means nobody has looked since the code changed, which is exactly the condition that let real
patent-specification claims drift out of sync with the code they described throughout the same
night this ADR is named for (see bug class 6, "Cross-document/cross-section self-contradiction,"
in the bug-hunt skill's own list).

Python only, for now, matching every other layer of this anchor system's own incremental rollout
(`check_function_test_anchors.py` shipped Python-only under ADR 0090 before its own JS and Rust
sub-tracks followed as separate, later work) -- reuses `check_function_test_anchors.py`'s own
`_function_span` so this check's notion of "the function's own text" never drifts from the
existing ratchet's, the same reasoning that script's own docstring gives for reusing it from
`check_anchor_coverage.py`. JS and Rust claims support are real, named, not-yet-done follow-ons.

No baseline/ratchet is needed the way `check_function_test_anchors.py` needs one: this is a brand
new mechanism with zero pre-existing claims when it ships, so there is no backlog to grandfather --
every claim ever committed should have an accurate hash from the moment it's added.
"""

import argparse
import ast
import hashlib
import os
import re
import subprocess
import sys

import verify_anchors as va  # noqa: E402
from check_function_test_anchors import (  # noqa: E402
    _direct_functions,
    _function_span,
    _is_base_anchor_declaration,
)

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?\n)---\s*\n", re.DOTALL)
FIELD_RE = re.compile(r"^([a-zA-Z_]+):\s*(.+?)\s*$", re.MULTILINE)


def _git_tracked_files(repo_root, suffix):
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "ls-files", f"*{suffix}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return []
    return [
        os.path.join(repo_root, line)
        for line in result.stdout.splitlines()
        if line.strip()
    ]


def parse_claim_frontmatter(content):
    """Returns {field: value} from a claim file's own YAML-lite frontmatter, or None if the
    file has no `---`-delimited frontmatter block at all. Deliberately not a real YAML parser --
    this format is two flat scalar fields (`anchor`, `code_hash`), not a dependency worth adding."""
    m = FRONTMATTER_RE.match(content)
    if not m:
        return None
    fields = {}
    for fm in FIELD_RE.finditer(m.group(1)):
        fields[fm.group(1)] = fm.group(2).strip().strip('"').strip("'")
    return fields


# [@ANCHOR: check_claims_freshness:COMM_claim_retirement]
def check_retirement(fields, claim_path, search_roots=()):
    """Classify a claim's retirement. Returns `(is_retired, problem_or_None)`.

    A claim documents one anchored function. When that function is deliberately deleted or
    renamed, the claim is kept on purpose -- it holds the original finding, often a tier-1
    security one -- but its `anchor:` can no longer resolve, so every freshness checker reported
    it as an "orphaned claim" forever. Three real claims were in exactly that state, against one
    genuine finding, and a gate that can never go green stops being read: the real orphan hiding
    among the permanent ones stops being noticed. There was also no way to fix it from inside the
    claim, because dropping `anchor:` only trades the error for "missing required frontmatter".

    The convention, therefore, is a `retired:` date plus a statement of WHICH kind of retirement
    it is, because a reviewer must be able to tell a deliberate retirement from an accidental
    orphan (bug classes 40 and 45 both manifest as exactly this symptom):

        retired: 2026-09-15
        superseded_by: _create_odoo_role_if_missing.md   # renamed or moved: name the live claim
        retired_reason: deleted                          # or: the code is simply gone

    Exactly one of the two is required. `retired:` alone is refused, so this cannot become a
    one-line way to silence a real orphan -- the author has to say which case it is, and when
    they say "superseded", the successor is verified to exist and to not itself be retired, so a
    typo or a retirement chain cannot quietly hide a claim forever.

    `superseded_by` is resolved relative to the retired claim's own directory first (the common
    case is a renamed function's sibling claim), then relative to each of `search_roots` (for a
    successor that moved to another module's claims directory).
    """
    if "retired" not in fields:
        return False, None

    superseded = fields.get("superseded_by")
    reason = fields.get("retired_reason")
    if not superseded and not reason:
        return True, (
            "'retired:' with neither 'superseded_by:' nor 'retired_reason:' -- a retirement must "
            "say which kind it is, so it can't be used to silence a genuinely orphaned claim; "
            "name the live successor claim, or give a reason such as 'retired_reason: deleted'"
        )

    if superseded:
        resolved, tried = _resolve_superseded_by(claim_path, superseded, search_roots)
        if resolved is None:
            return True, (
                f"superseded_by '{superseded}' names no existing claim file (looked in: "
                f"{', '.join(tried)}) -- a typo here hides this claim forever"
            )
        try:
            with open(resolved, "r", encoding="utf-8") as f:
                successor = parse_claim_frontmatter(f.read())
        except (OSError, UnicodeDecodeError):
            successor = None
        if successor is not None and "retired" in successor:
            return True, (
                f"superseded_by '{superseded}' is itself retired -- point at the live claim that "
                "documents the current code, not at another retired one"
            )

    return True, None


def _resolve_superseded_by(claim_path, value, search_roots):
    """Returns `(resolved_path_or_None, paths_tried)` for a `superseded_by:` value."""
    tried = []
    candidates = [os.path.join(os.path.dirname(claim_path), value)]
    candidates.extend(os.path.join(root, value) for root in search_roots)
    for candidate in candidates:
        normalized = os.path.normpath(candidate)
        tried.append(normalized)
        if os.path.isfile(normalized):
            return normalized, tried
    return None, tried


def compute_function_hash(filepath):
    """Returns {anchor_name: sha256_hex} for every function in `filepath` carrying a real BASE
    anchor declaration (not a Tests/Verified-by/Triggers link to one) within its own span."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        tree = ast.parse(content, filename=filepath)
    except SyntaxError:
        return {}

    lines = content.splitlines()
    hashes = {}
    for _qualname, node in _direct_functions(tree.body, []):
        start, end = _function_span(node, lines)
        span_lines = lines[start - 1 : min(end, len(lines))]
        span = "\n".join(span_lines)
        for i, line in enumerate(span_lines):
            prev_line = span_lines[i - 1] if i > 0 else ""
            for anchor_match in va.ANCHOR_PATTERN.finditer(line):
                if not _is_base_anchor_declaration(line, anchor_match, prev_line):
                    continue
                name = va._clean(anchor_match.group(1))
                hashes[name] = hashlib.sha256(span.encode("utf-8")).hexdigest()
    return hashes


def build_anchor_hash_index(repo_root):
    """Returns {anchor_name: sha256_hex} across every git-tracked .py file in repo_root. A
    duplicate anchor name across files is a pre-existing verify_anchors.py-level problem, not
    this script's to resolve -- last one scanned wins, matching dict-update semantics elsewhere
    in this same tool family."""
    index = {}
    for filepath in _git_tracked_files(repo_root, ".py"):
        index.update(compute_function_hash(filepath))
    return index


def check_claims(repo_root):
    """Returns a list of (claim_path, problem) for every claims/*.md file whose own recorded
    hash doesn't match the anchor's current code, or whose anchor no longer exists at all.

    Thin wrapper over `scan_claims`, kept because it is this module's long-standing entry point.
    """
    return scan_claims(repo_root)[0]


def scan_claims(repo_root):
    """Returns `(problems, retired)`: the same (claim_path, problem) list `check_claims` returns,
    plus the paths of every claim deliberately retired (see COMM_claim_retirement). A retired
    claim is not hash-checked -- its anchor is gone on purpose -- but is reported as a non-failing
    count, so retirements stay visible instead of disappearing."""
    problems = []
    retired = []
    anchor_hashes = None  # computed lazily, only if a claim file actually exists

    for claim_path in _git_tracked_files(repo_root, ".md"):
        if f"{os.sep}claims{os.sep}" not in claim_path:
            continue
        # A README.md inside a claims/ directory is a deliberate index/explanation file for the
        # directory itself (e.g. hardware/claims/README.md: "reviewed, out of scope for the
        # bug-hunt campaign"), not a claim -- it carries no frontmatter because it isn't one.
        # Confirmed live: this was a real false positive ("missing required frontmatter") against
        # the real hams_com repo before this exclusion.
        if os.path.basename(claim_path) == "README.md":
            continue
        # The centralized claims store (docs/bug_hunt_claims/<repo>/...) documents code that
        # lives in a DIFFERENT git repo (hams_open/hams_shared claims, centralized into hams_com
        # per the bug-hunt skill's own store policy) -- this checker's whole freshness model
        # assumes the claim and the anchored code share one repo root (`build_anchor_hash_index`
        # only ever scans `repo_root`'s own git-tracked .py files), so it can never find a
        # centralized claim's real anchor and would otherwise always report it "orphaned". The
        # bug-hunt skill's own docs already name the correct tool for these:
        # `check_claims.py --source-root <other-repo>`. Confirmed live: before this exclusion,
        # running this checker against the real hams_com repo for the first time ever (it has
        # never been wired into run_linters.py -- ADR 0091 names this as a real, tracked
        # follow-on) produced over 1100 false-positive "orphaned claim" reports, the large
        # majority of them centralized-store claims for hams_open/hams_shared code.
        rel = os.path.relpath(claim_path, repo_root)
        if rel.split(os.sep)[:2] == ["docs", "bug_hunt_claims"]:
            continue
        try:
            with open(claim_path, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        fields = parse_claim_frontmatter(content)
        if fields is None or "anchor" not in fields or "code_hash" not in fields:
            problems.append(
                (claim_path, "missing required frontmatter fields 'anchor' and/or 'code_hash'")
            )
            continue

        # Checked before the hash, deliberately: a retired claim's anchor is gone on purpose, so
        # every check below it would report a permanent, unfixable failure. See
        # COMM_claim_retirement.
        is_retired, retirement_problem = check_retirement(fields, claim_path, (repo_root,))
        if is_retired:
            retired.append(claim_path)
            if retirement_problem:
                problems.append((claim_path, retirement_problem))
            continue

        recorded = fields["code_hash"]
        if recorded.strip().lower().startswith("not computable"):
            # Established convention (already in active use across the real repo -- every
            # Rust-anchored claim in daemons/*/claims/, plus claims for genuinely un-anchored
            # Python functions) for a claim this checker structurally cannot verify yet: Python-
            # only anchor hashing (per this file's own docstring, "JS and Rust claims support are
            # real, named, not-yet-done follow-ons") has no way to resolve a Rust/JS anchor, and a
            # function with no real `[@ANCHOR:]` at all has nothing to hash regardless. Before
            # this check, such a claim always fell through to the "anchor not found" branch below
            # and was reported as "orphaned" -- a false positive, and a misleading one (implying
            # the anchor was deleted, when it was simply never Python). Confirmed live: ALL 344 of
            # the real hams_com repo's post-centralized-store-fix "orphaned claim" findings used
            # this exact convention, none of them genuinely orphaned.
            continue

        if anchor_hashes is None:
            anchor_hashes = build_anchor_hash_index(repo_root)

        anchor = fields["anchor"]
        current = anchor_hashes.get(va._clean(anchor))
        if current is None:
            problems.append(
                (claim_path, f"anchor '{anchor}' not found in any git-tracked .py file -- orphaned claim")
            )
            continue

        expected = recorded[len("sha256:") :] if recorded.startswith("sha256:") else recorded
        if expected != current:
            problems.append(
                (
                    claim_path,
                    f"code_hash mismatch for anchor '{anchor}' -- the function changed since this "
                    f"claim was last confirmed (recorded sha256:{expected[:12]}..., current "
                    f"sha256:{current[:12]}...); review the claim's own prose against the current "
                    "code and update code_hash once confirmed accurate (or fix the code, if the "
                    "claim was right and the change broke it)",
                )
            )

    return problems, retired


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", nargs="?", default=".")
    args = parser.parse_args()

    repo_root = os.path.abspath(args.directory)
    # A path that isn't a directory walks nothing and would otherwise print SUCCESS. On
    # 2026-09-16 a run from the wrong working directory passed a module name that didn't
    # resolve, and the reassuring result was read as the six stale claims being fixed.
    if not os.path.isdir(repo_root):
        print(f"[!] ERROR: {args.directory!r} is not a directory (resolved to {repo_root}).")
        return 2
    problems, retired = scan_claims(repo_root)

    if retired:
        print(
            f"[*] {len(retired)} retired claim(s) skipped (see COMM_claim_retirement) -- kept as "
            "historical record, not checked against current code:"
        )
        for claim_path in retired:
            print(f"    - {os.path.relpath(claim_path, repo_root)}")

    if problems:
        print("[!] CI/CD FAILURE: Stale or Orphaned Function Claims Detected (ADR 0091):")
        for claim_path, problem in problems:
            rel = os.path.relpath(claim_path, repo_root)
            print(f"    - {rel}: {problem}")
        return 1

    print("[+] SUCCESS: All function claims match their anchored code's current hash.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
