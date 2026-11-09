#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
One-sweep migration of bug-hunt claim `code_hash` values to the comment- and docstring-insensitive
definition (2026-11-09; see `check_claims_freshness.normalized_span_text`).

Why it exists: before the change, `code_hash` was the sha256 of the function's raw source text, so
rewording a comment staled the claim. One night of comment-rewording pull requests staled 27
user_websites claims and left about 195 mismatches repo-wide. The new definition ignores comments,
docstrings, blank lines and trailing whitespace, so a comment-only change no longer needs a
re-check; this script recomputes the existing hashes once.

For every live claim (retired and `not computable` claims are skipped) it classifies the recorded hash:

  current       already the normalized hash of the anchor's current code. Nothing to do.
  legacy        the old raw-text hash of the current code: the code is unchanged since the claim
                was confirmed. The hash is rewritten to the normalized form.
  comment-only  neither, but the git history of the anchor's file has a revision whose raw-text
                (or normalized) hash is the recorded one AND whose normalized hash equals the
                current one: only comments, docstrings or whitespace changed since the claim was
                confirmed. The hash is rewritten. (This is what clears the claims the
                comment-rewording pull requests staled.)
  stale         the recorded hash matches an older revision whose code differs from today's, or
                matches nothing in the searched history. Needs a human or agent to re-derive the
                claim; listed, never rewritten.
  orphan        no anchor of that name in any source root. Listed, never rewritten.

Without `--apply` it only reports. Run it from a CLEAN checkout of main (a working tree with edits
would record the edits' hash):

    python3 hams_shared/tools/migrate_claim_hashes.py \
        --claims-dir hams_com/docs/bug_hunt_claims --claims-dir hams_com \
        --source-root hams_com --source-root hams_open [--apply]

`--source-root` may be repeated; each root's `hams_shared` submodule, when it is its own git
checkout, is scanned too. Compatibility: until this script has been run over every claim store and
the open claim pull requests have merged, `check_claims_freshness.recorded_hash_matches` also
accepts a legacy hash whose function text is unchanged; delete that branch afterwards.
"""

import argparse
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import verify_anchors as va  # noqa: E402
from check_claims_freshness import (  # noqa: E402
    _git_tracked_files,
    check_retirement,
    function_hashes_from_text,
    parse_claim_frontmatter,
)

HASH_LINE_RE = re.compile(r"^(code_hash:[ \t]*)(\S.*?)[ \t]*$", re.MULTILINE)
DEFAULT_HISTORY_DEPTH = 300


def source_roots_with_submodule(roots):
    """Each root, followed by its `hams_shared` submodule when that is its own git checkout."""
    out = []
    for root in roots:
        root = os.path.abspath(root)
        out.append(root)
        shared = os.path.join(root, "hams_shared")
        if os.path.exists(os.path.join(shared, ".git")) and not os.path.islink(shared):
            out.append(shared)
    return out


def build_index(roots):
    """{anchor: (repo_root, relative_path, normalized_hash, legacy_hash)}. Last scanned wins."""
    index = {}
    for root in roots:
        for path in _git_tracked_files(root, ".py"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            for name, (norm, legacy) in function_hashes_from_text(content, path).items():
                index[name] = (root, os.path.relpath(path, root), norm, legacy)
    return index


def _git(root, *args):
    result = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def history_verdict(root, rel_path, anchor, recorded, current_norm, depth):
    """Walks the newest `depth` revisions of `rel_path`. Returns ("comment-only", rev) when a
    revision carries the recorded hash and has the current normalized hash, ("changed", rev) when
    it carries the recorded hash but different code, or (None, None) when no revision matches."""
    revs = _git(root, "log", f"-n{depth}", "--format=%H", "--", rel_path)
    for rev in (revs or "").split():
        text = _git(root, "show", f"{rev}:{rel_path}")
        if text is None:
            continue
        pair = function_hashes_from_text(text, rel_path).get(anchor)
        if pair is None:
            continue
        norm, legacy = pair
        if recorded in (norm, legacy):
            return ("comment-only" if norm == current_norm else "changed"), rev
    return None, None


def claim_files(claims_dirs):
    seen = set()
    for base in claims_dirs:
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "target")]
            if os.path.basename(dirpath) != "claims":
                continue
            for name in files:
                path = os.path.join(dirpath, name)
                if name.endswith(".md") and name != "README.md" and path not in seen:
                    seen.add(path)
                    yield path


def migrate(claims_dirs, source_roots, apply=False, depth=DEFAULT_HISTORY_DEPTH, out=print):
    """Returns {"current": [...], "legacy": [...], "comment-only": [...], "stale": [...],
    "orphan": [...]} of claim paths (stale and orphan entries are `(path, detail)`)."""
    roots = source_roots_with_submodule(source_roots)
    index = build_index(roots)
    result = {k: [] for k in ("current", "legacy", "comment-only", "stale", "orphan")}
    for path in sorted(claim_files(claims_dirs)):
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        fields = parse_claim_frontmatter(content)
        if fields is None or "anchor" not in fields or "code_hash" not in fields:
            continue
        if check_retirement(fields, path, tuple(claims_dirs))[0]:
            continue
        recorded_full = fields["code_hash"]
        if recorded_full.strip().lower().startswith("not computable"):
            continue
        recorded = recorded_full[len("sha256:") :] if recorded_full.startswith("sha256:") else recorded_full
        anchor = va._clean(fields["anchor"])
        entry = index.get(anchor)
        if entry is None:
            result["orphan"].append((path, fields["anchor"]))
            continue
        root, rel, norm, legacy = entry
        if recorded == norm:
            result["current"].append(path)
            continue
        if recorded == legacy:
            kind = "legacy"
        else:
            verdict, rev = history_verdict(root, rel, anchor, recorded, norm, depth)
            if verdict != "comment-only":
                detail = (
                    f"{rel}: code changed since {rev[:10]}" if verdict == "changed"
                    else f"{rel}: recorded hash found in none of the last {depth} revisions"
                )
                result["stale"].append((path, detail))
                continue
            kind = "comment-only"
        result[kind].append(path)
        if apply:
            new = HASH_LINE_RE.sub(lambda m: f"{m.group(1)}sha256:{norm}", content, count=1)
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
    for kind in ("current", "legacy", "comment-only", "stale", "orphan"):
        out(f"[*] {kind}: {len(result[kind])}" + (" (rewritten)" if apply and kind in ("legacy", "comment-only") else ""))
    for kind in ("stale", "orphan"):
        for path, detail in result[kind]:
            out(f"    {kind}: {path}: {detail}")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--claims-dir", action="append", required=True)
    parser.add_argument("--source-root", action="append", required=True)
    parser.add_argument("--apply", action="store_true", help="rewrite the hashes (default: report only)")
    parser.add_argument("--history-depth", type=int, default=DEFAULT_HISTORY_DEPTH)
    args = parser.parse_args(argv)
    for d in args.claims_dir + args.source_root:
        if not os.path.isdir(d):
            print(f"[!] ERROR: {d!r} is not a directory.")
            return 2
    result = migrate([os.path.abspath(d) for d in args.claims_dir], args.source_root, args.apply, args.history_depth)
    return 1 if (result["stale"] or result["orphan"]) else 0


if __name__ == "__main__":
    sys.exit(main())
