#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Nested Template Literal Linter (rjsmin compatibility)
-------------------------------------------------------
Odoo's asset pipeline (odoo/addons/base/models/assetsbundle.py) always
re-minifies every JS file listed under any manifest "assets" bundle key
with rjsmin, including already-minified vendor files. rjsmin's own docs
state it only supports "(Unnested) template literals" -- its string-
matching regex has no notion of `${}` nesting, so on a template literal
whose substitution itself contains another template literal (e.g. a
ternary branch that is itself a backtick string), rjsmin closes the
OUTER literal at the first backtick it finds inside the inner one and
resumes normal-mode parsing right there. Everything minified afterward
in that bundle is corrupted; the corruption doesn't surface until
whatever code happens to sit later in the same file hits its own,
perfectly ordinary backtick -- which is exactly what produced the
systemic "Uncaught SyntaxError: Unexpected identifier 'Unexpected'"
browser-tour failures traced back to ham_events/static/src/lib/
transformers.min.js (see hams_com commit f1f00511).

This does NOT flag ordinary (unnested) template literals -- those are
explicitly safe per rjsmin's own documented support, and flagging every
template literal in every bundled JS file would be enormous, noisy, and
untrue to the actual defect class. It flags only genuine nesting: a
backtick opened while already inside another template literal's `${...}`
substitution.
"""

import os
import sys
import ast


def _resolve_repo_root(given_path):
    """run_linters.py's own `dir_path` resolves to the hams_shared directory itself, not a real
    repo root (same bug found and fixed in check_model_extension_collisions.py and others) --
    confirmed directly: this checker was silently finding 0 bundled JS assets via run_linters.py's
    actual invocation, versus 48 at a real repo root. Detect the hams_shared case by name and
    redirect to its real parent repo."""
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        return os.path.dirname(given_path)
    return given_path


def _find_sibling_repo(repo_root):
    """Mirrors check_dependency_cycles.py's own sibling-repo resolution. Computed internally
    rather than only trusting sys.argv[2:] -- run_linters.py's own `sibling_dir` for this step is
    derived from the same wrong `dir_path` (see _resolve_repo_root above), so a caller-supplied
    sibling arg can be wrong even after repo_root itself is fixed; computing it here too means
    this script is correct regardless of what run_linters.py passes."""
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


def find_nested_template_literals(code):
    """Scan JS source for backticks opened inside a `${...}` substitution
    of an already-open template literal. Returns a list of (line, col)
    1-indexed positions of each offending inner backtick.

    This is a best-effort character scanner, not a real JS parser: it
    does not disambiguate regex literals from division, so a regex
    literal containing a brace (e.g. `/{2,3}/`) inside a `${...}`
    substitution can desync the brace-depth count. That combination is
    rare enough in practice that a false positive/negative there is an
    acceptable tradeoff for staying dependency-free and fast.
    """
    findings = []
    stack = []  # each frame: [submode] where submode is "TEXT" or a dict for EXPR depth
    i = 0
    n = len(code)
    line = 1
    line_start = 0

    def pos(idx):
        # 1-indexed (line, col) for reporting.
        return line, idx - line_start + 1

    while i < n:
        c = code[i]
        if c == "\n":
            line += 1
            line_start = i + 1

        if stack and stack[-1]["mode"] == "TEXT":
            if c == "\\":
                i += 2
                continue
            if c == "`":
                stack.pop()
                i += 1
                continue
            if c == "$" and i + 1 < n and code[i + 1] == "{":
                stack[-1]["mode"] = "EXPR"
                stack[-1]["depth"] = 0
                i += 2
                continue
            i += 1
            continue

        if stack and stack[-1]["mode"] == "EXPR":
            if c == "`":
                l, col = pos(i)
                findings.append((l, col))
                stack.append({"mode": "TEXT"})
                i += 1
                continue
            if c in ("'", '"'):
                quote = c
                i += 1
                while i < n and code[i] != quote:
                    if code[i] == "\\":
                        i += 2
                    else:
                        if code[i] == "\n":
                            line += 1
                            line_start = i + 1
                        i += 1
                i += 1
                continue
            if c == "/" and i + 1 < n and code[i + 1] == "/":
                nl = code.find("\n", i)
                i = nl if nl != -1 else n
                continue
            if c == "/" and i + 1 < n and code[i + 1] == "*":
                end = code.find("*/", i + 2)
                segment = code[i:end if end != -1 else n]
                line += segment.count("\n")
                if "\n" in segment:
                    line_start = i + segment.rfind("\n") + 1
                i = end + 2 if end != -1 else n
                continue
            if c == "{":
                stack[-1]["depth"] += 1
                i += 1
                continue
            if c == "}":
                if stack[-1]["depth"] == 0:
                    stack[-1]["mode"] = "TEXT"
                else:
                    stack[-1]["depth"] -= 1
                i += 1
                continue
            i += 1
            continue

        # Top-level JS, no open template literal.
        if c == "`":
            stack.append({"mode": "TEXT"})
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            i += 1
            while i < n and code[i] != quote:
                if code[i] == "\\":
                    i += 2
                else:
                    if code[i] == "\n":
                        line += 1
                        line_start = i + 1
                    i += 1
            i += 1
            continue
        if c == "/" and i + 1 < n and code[i + 1] == "/":
            nl = code.find("\n", i)
            i = nl if nl != -1 else n
            continue
        if c == "/" and i + 1 < n and code[i + 1] == "*":
            end = code.find("*/", i + 2)
            segment = code[i:end if end != -1 else n]
            line += segment.count("\n")
            if "\n" in segment:
                line_start = i + segment.rfind("\n") + 1
            i = end + 2 if end != -1 else n
            continue
        i += 1

    return findings


def collect_minified_js_assets(repo_root):
    """Map every .js path listed under any manifest "assets" bundle key
    to the list of bundle names it appears in. All such files are
    re-minified by Odoo's asset pipeline uniformly (assetsbundle.py's
    is_minified flag is a per-request/per-bundle-build setting, not a
    per-file opt-out), so there is no bundle key that is safe to skip.
    """
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


def main():
    if len(sys.argv) < 2:
        print("Usage: check_minified_js_nested_templates.py <repo_root> [sibling_repo_root]")
        sys.exit(1)

    repo_root = _resolve_repo_root(sys.argv[1])
    search_roots = [repo_root] + sys.argv[2:]
    computed_sibling = _find_sibling_repo(repo_root)
    if computed_sibling and computed_sibling not in search_roots:
        search_roots.append(computed_sibling)

    asset_to_bundles = collect_minified_js_assets(repo_root)
    if not asset_to_bundles:
        print("[+] Nested Template Literal Linter: No bundled JS assets found.")
        sys.exit(0)

    any_failed = False
    checked = 0
    for asset_path, bundles in sorted(asset_to_bundles.items()):
        real_path = resolve_asset_path(asset_path, search_roots)
        if real_path is None:
            # Referenced from another installed module outside this repo's
            # own tree and the given sibling root -- not ours to check here.
            continue
        try:
            with open(real_path, "r", encoding="utf-8") as f:
                code = f.read()
        except (OSError, UnicodeDecodeError) as e:
            print(f"❌ ERROR reading {real_path}: {e}")
            any_failed = True
            continue
        checked += 1
        findings = find_nested_template_literals(code)
        if findings:
            any_failed = True
            print(f"🚨 NESTED TEMPLATE LITERAL (unsafe for Odoo's rjsmin minifier) in {asset_path}")
            print(f"  Bundled under: {', '.join(bundles)}")
            for l, col in findings[:10]:
                print(f"  {asset_path}:{l}:{col}")
            if len(findings) > 10:
                print(f"  ... and {len(findings) - 10} more")
            print(
                "  Fix: flatten the nested template literal to string concatenation, or\n"
                "  stop bundling this file (declare it outside any \"assets\" manifest key\n"
                "  and load it as a raw <script> tag) so rjsmin never re-minifies it.\n"
                "  See ham_events/__manifest__.py's comment on transformers.min.js for\n"
                "  a worked example of the second approach."
            )

    if any_failed:
        print("\n🛑 Halting due to nested template literals in minified JS assets.")
        sys.exit(1)

    print(f"[+] Nested Template Literal Linter: {checked} bundled JS asset(s) checked, all clean.")
    sys.exit(0)


if __name__ == "__main__":
    main()
