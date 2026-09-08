#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Minified JS Output Syntax Linter (real rjsmin, real Node parser)
------------------------------------------------------------------
check_minified_js_nested_templates.py already catches one specific, documented
class of rjsmin miscompilation (nested template literals -- see that script's
own docstring and hams_com commit f1f00511 for the real production bug that
motivated it). That check works by pattern-matching the SOURCE for the one
known-bad construct, which only ever protects against defect classes someone
has already found and named.

This check instead validates the actual OUTPUT of Odoo's real minifier
directly: for every JS file any manifest lists under an "assets" bundle key,
it runs the exact same call Odoo's own asset pipeline makes
(`rjsmin.jsmin(content)`, per
`odoo/addons/base/models/assetsbundle.py`'s `JavascriptAsset.minify()`) and
then feeds the result through a real JS parser (`node --check`, the same
technique check_js_syntax.py already uses on source files) to confirm the
minifier didn't produce something a browser can no longer parse. This is a
general backstop for ANY future rjsmin miscompilation, not just the one
pattern already known -- rjsmin is a fast, deliberately naive regex-based
minifier with no real JS grammar behind it, so new defect classes (a comment
inside a regex literal, an ASI hazard exposed by whitespace removal, etc.)
are a real, open-ended risk category, not a closed list.

Note the narrower scope versus a hypothetical "does this bundle behave
identically after minification" check: rjsmin can still produce
semantically-different-but-syntactically-VALID output (a defect this check
cannot see) -- this only catches the case where minification breaks parsing
outright. Keep check_minified_js_nested_templates.py's targeted check
alongside this one: it gives a precise, actionable diagnosis (an exact
offending backtick position) for the one defect class it knows about, where
this check only reports whatever Node's parser says about the byte offset in
the minified (whitespace-stripped, hard for a human to read) output.
"""

import os
import sys
import subprocess
import multiprocessing

import rjsmin


def _resolve_repo_root(given_path):
    """run_linters.py's own `dir_path` resolves to the hams_shared directory itself, not a real
    repo root (same bug found and fixed in check_model_extension_collisions.py,
    check_minified_js_nested_templates.py, and others) -- detect the hams_shared case by name and
    redirect to its real parent repo."""
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        return os.path.dirname(given_path)
    return given_path


def _find_sibling_repo(repo_root):
    """Mirrors check_minified_js_nested_templates.py's own sibling-repo resolution."""
    repo_root = os.path.abspath(repo_root)
    for sibling_name in ("hams_open", "hams_com"):
        if os.path.basename(repo_root) == sibling_name:
            continue
        candidate = os.path.abspath(os.path.join(repo_root, "..", sibling_name))
        if not os.path.isdir(candidate):
            continue
        has_a_module = any(
            os.path.isfile(os.path.join(candidate, d, "__manifest__.py"))
            for d in os.listdir(candidate)
            if os.path.isdir(os.path.join(candidate, d))
        )
        if has_a_module:
            return candidate
    return None


def collect_minified_js_assets(repo_root):
    """Map every .js path listed under any manifest "assets" bundle key to the list of bundle
    names it appears in -- identical logic to check_minified_js_nested_templates.py's own
    collector (kept as a separate copy rather than a shared import, matching this codebase's own
    established convention of each check_*.py tool staying independently invocable)."""
    import ast

    asset_to_bundles = {}
    for root, dirs, files in os.walk(repo_root):
        if "radae" in dirs:
            dirs.remove("radae")
        dirs[:] = [d for d in dirs if d not in ("node_modules", "__pycache__") and not d.startswith(".")]
        if "__manifest__.py" not in files:
            continue
        manifest_path = os.path.join(root, "__manifest__.py")
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=manifest_path)
        except (SyntaxError, OSError) as e:
            print(f"❌ ERROR parsing {manifest_path}: {e}")
            continue
        for node in tree.body:
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Dict)):
                continue
            try:
                manifest_dict = ast.literal_eval(node.value)
            except ValueError:
                continue
            assets = manifest_dict.get("assets", {})
            if not isinstance(assets, dict):
                continue
            for bundle_name, file_list in assets.items():
                if not isinstance(file_list, list):
                    continue
                for asset_path in file_list:
                    if isinstance(asset_path, str) and asset_path.endswith(".js"):
                        asset_to_bundles.setdefault(asset_path, []).append(bundle_name)
    return asset_to_bundles


def resolve_asset_path(asset_path, search_roots):
    for root in search_roots:
        candidate = os.path.join(root, asset_path)
        if os.path.isfile(candidate):
            return candidate
    return None


def _check_one(args):
    asset_path, real_path = args
    try:
        with open(real_path, "r", encoding="utf-8") as f:
            code = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return (asset_path, real_path, f"could not read file: {e}")

    try:
        minified = rjsmin.jsmin(code)
    except Exception as e:  # rjsmin itself is a regex engine, not immune to raising
        return (asset_path, real_path, f"rjsmin.jsmin() itself raised: {e}")

    res = subprocess.run(
        ["node", "--input-type=module", "--check"],
        input=minified,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        err = res.stderr.replace("[stdin]", os.path.basename(real_path)).strip()
        return (asset_path, real_path, err)
    return None


def main():
    if len(sys.argv) < 2:
        print("Usage: check_minified_js_syntax_errors.py <repo_root> [sibling_repo_root]")
        sys.exit(1)

    repo_root = _resolve_repo_root(sys.argv[1])
    search_roots = [repo_root] + sys.argv[2:]
    computed_sibling = _find_sibling_repo(repo_root)
    if computed_sibling and computed_sibling not in search_roots:
        search_roots.append(computed_sibling)

    asset_to_bundles = collect_minified_js_assets(repo_root)
    if not asset_to_bundles:
        print("[+] Minified JS Output Syntax Linter: No bundled JS assets found.")
        sys.exit(0)

    jobs = []
    for asset_path in sorted(asset_to_bundles):
        real_path = resolve_asset_path(asset_path, search_roots)
        if real_path is None:
            # Referenced from another installed module outside this repo's own tree and the
            # given sibling root -- not ours to check here.
            continue
        jobs.append((asset_path, real_path))

    if not jobs:
        print("[+] Minified JS Output Syntax Linter: No resolvable bundled JS assets found.")
        sys.exit(0)

    print(f"[*] Minified JS Output Syntax Linter: Checking {len(jobs)} bundled JS asset(s) against real rjsmin output...")

    pool_size = min(4, multiprocessing.cpu_count() or 1)
    errors = []
    with multiprocessing.Pool(pool_size) as pool:
        for res in pool.imap_unordered(_check_one, jobs):
            if res:
                errors.append(res)

    if errors:
        print("🚨 REAL MINIFICATION PRODUCES INVALID JAVASCRIPT 🚨\n")
        for asset_path, real_path, err in sorted(errors):
            bundles = ", ".join(asset_to_bundles[asset_path])
            print(f"File: {asset_path} (bundled under: {bundles})")
            print(f"  Source: {real_path}")
            print(f"  {err}")
            print("-" * 60)
        print(
            "\n🛑 Halting: Odoo's real rjsmin minifier (rjsmin.jsmin, the exact call\n"
            "JavascriptAsset.minify() makes) turns one of these files into JS a real\n"
            "parser rejects. This is a genuine ship-breaking bug -- every user gets the\n"
            "minified bundle in production, never the raw source. See\n"
            "check_minified_js_nested_templates.py for the one already-diagnosed cause\n"
            "of this defect class; if that check is clean here too, this is a new one."
        )
        sys.exit(1)

    print(f"[+] Minified JS Output Syntax Linter: {len(jobs)} bundled JS asset(s) checked, all parse clean after real minification.")
    sys.exit(0)


if __name__ == "__main__":
    main()
