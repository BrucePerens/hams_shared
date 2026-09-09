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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
        for line in span_lines:
            for anchor_match in va.ANCHOR_PATTERN.finditer(line):
                if not _is_base_anchor_declaration(line, anchor_match):
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
    hash doesn't match the anchor's current code, or whose anchor no longer exists at all."""
    problems = []
    anchor_hashes = None  # computed lazily, only if a claim file actually exists

    for claim_path in _git_tracked_files(repo_root, ".md"):
        if f"{os.sep}claims{os.sep}" not in claim_path:
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

        if anchor_hashes is None:
            anchor_hashes = build_anchor_hash_index(repo_root)

        anchor = fields["anchor"]
        recorded = fields["code_hash"]
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

    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", nargs="?", default=".")
    args = parser.parse_args()

    repo_root = os.path.abspath(args.directory)
    problems = check_claims(repo_root)

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
