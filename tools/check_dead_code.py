#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Dead-Code Reachability Checker (website templates, backend views, JS static files)
-------------------------------------------------------------------------------------------
2026-08-31: built after AUDIT_IGNORE_VIEW_UNVERIFIED_CLAIMS.md's cluster triage manually found
two real dead pages (ham_onboarding/views/elmer_directory_templates.xml,
alias_portal_templates.xml -- no controller route served either, no menu linked to either, no
JS referenced either) with zero help from the existing linter suite. verify_anchors.py's
"orphan" checks are about test-documentation traceability (does a `[@ANCHOR: name]` have a
matching `# Tests [@ANCHOR: name]`), not reachability -- a page can be perfectly documented and
tested-by-anchor-name while still being genuinely dead. check_burn_list.py has exactly one
narrow, unrelated dead-code rule (an XML tour class never targeted by any JS tour). Nothing
general existed before this file.

A real study of off-the-shelf tools (Vulture, ruff, coverage.py) confirmed none of them fit:
Vulture is Python-only and flags nearly every Odoo model/field/controller as "unused" because
ORM field access and HTTP routing are both string/decorator-driven, not direct Python calls --
tested live against ham_onboarding/, produced dozens of false positives on real, live code in
seconds, and still can't see XML at all, so it would never have caught either real dead
template. ruff's dead-code-adjacent rules (F401/F811) are the same class of check
run_linters.py's flake8 step already runs, and are Python-only for the same reason. coverage.py
instruments executed Python bytecode -- these two dead templates had no Python code path behind
them at all ("no controller route serves it"), so there was nothing for it to ever mark as
uncovered.

## What this checks

Three declaration kinds, each checked against the same question: does this id/filename appear
as a reference ANYWHERE ELSE in the repo (any .py/.xml/.js file), outside its own declaring
line? This is deliberately the same low-tech, high-confidence signal that caught both real dead
files by hand tonight ("grepped the whole repo, nothing") -- not a fully-modeled simulation of
Odoo's view-resolution or JS bundling algorithm, which would be far more work and far more
fragile to get exactly right. Being generous about what counts as a "reference" (any textual
occurrence, not just a specific XML attribute or specific Python call shape) is a deliberate
choice: false negatives (missing a real dead file) are cheap here, since this check is
advisory, not a required CI gate yet; false positives (flagging a real, reachable page as dead)
are expensive, since they erode trust in the tool and risk a real deletion of live code. See
run_linters.py's own docstring on step ordering for how a future, more mature version of this
check could eventually be promoted to a hard gate once its false-positive rate on the real repo
is known and bounded -- not attempted here.

1. **Website templates**: `<template id="X" ...>` records under any `views/*.xml`. X (bare, or
   `module.X` module-qualified) must appear elsewhere -- typically inside a controller's
   `request.render("module.X", ...)` call, but any textual occurrence counts.
2. **Backend views**: `<record id="X" model="ir.ui.view">` records. Same reachability question,
   generous about how X gets referenced (action `view_id`, `inherit_id`, `env.ref()` from a
   Python method's dynamically-returned client action, etc. -- all just textual occurrences of
   the id elsewhere).
3. **JS static files**: every `.js` file under a module's `static/src/js/`, excluding
   `*.test.js` (test files are their own consumer, not something else's dependency) and files
   under a `static/tests/` or `static/tests/tours/` directory. A JS file's bare filename (e.g.
   `rx_noise_gate_processor.js`) must appear elsewhere -- in `__manifest__.py`'s asset bundle
   lists (the common case), in another JS file's `@module/js/name` import, or in a literal
   static-URL string (the AudioWorkletProcessor case: `rx_noise_gate_processor.js` is
   deliberately excluded from every asset bundle, loaded instead via
   `audioWorklet.addModule("/ham_shack/static/src/js/rx_noise_gate_processor.js")` -- a plain
   textual occurrence of the filename, which this check's generic "does the filename appear
   anywhere" question catches without needing special-case logic for addModule()).

Usage: check_dead_code.py <repo_root>
"""

import os
import re
import sys

SKIP_DIRS = {"node_modules", "__pycache__", ".git", "radae", "external"}
EXCLUDE_TOP_LEVEL = {"archive", "scratch"}


def _resolve_repo_root(given_path):
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        return os.path.dirname(given_path)
    return given_path


def _resolve_repo_roots(given_path):
    """Same sibling-repo shape as check_gdpr_erasure_uses_service_utility.py -- real modules
    with real templates/views/JS live in both hams_com and hams_open, and a reference to one
    module's declaration can legitimately live in the OTHER repo (e.g. a shared daemon or
    cross-repo controller), so the reference scan must cover both roots even when only one was
    given as the scan target."""
    repo_root = _resolve_repo_root(given_path)
    roots = [repo_root]
    sibling_name = "hams_open" if os.path.basename(repo_root) != "hams_open" else "hams_com"
    sibling = os.path.abspath(os.path.join(repo_root, "..", sibling_name))
    if os.path.isdir(sibling) and any(
        os.path.isfile(os.path.join(sibling, d, "__manifest__.py"))
        for d in os.listdir(sibling)
        if os.path.isdir(os.path.join(sibling, d))
    ):
        roots.append(sibling)
    return roots


def _get_module(path, repo_root):
    """Resolves the Odoo module a file belongs to by walking up to the nearest
    __manifest__.py -- same approach as verify_anchors.py's get_module(), simplified since this
    checker only needs the module name, not the full doc/global fallback chain."""
    current = os.path.dirname(os.path.abspath(path))
    while current and current != os.path.dirname(current):
        if os.path.exists(os.path.join(current, "__manifest__.py")):
            return os.path.basename(current)
        current = os.path.dirname(current)
    return None


def _iter_module_dirs(repo_root):
    for entry in sorted(os.listdir(repo_root)):
        if entry in EXCLUDE_TOP_LEVEL or entry.startswith("."):
            continue
        full = os.path.join(repo_root, entry)
        if os.path.isdir(full) and os.path.isfile(os.path.join(full, "__manifest__.py")):
            yield entry, full


def _iter_all_files(repo_root, extensions):
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for filename in files:
            if filename.endswith(extensions):
                yield os.path.join(root, filename)


TEMPLATE_ID_RE = re.compile(r'<template\s+[^>]*\bid="([^"]+)"')
VIEW_RECORD_RE = re.compile(
    r'<record\s+[^>]*\bid="([^"]+)"[^>]*\bmodel="ir\.ui\.view"'
)


def _find_template_declarations(path, module):
    """Skips any <template> that carries its own inherit_id attribute -- a real, systematic
    false-positive source found live on the first real-repo run: a template that inherits
    another (e.g. <template id="user_navbar_inherit_logbook"
    inherit_id="user_websites.user_navbar">) is auto-applied by Odoo whenever its base template
    loads. It needs no one to reference ITS OWN id to be reachable -- the reference relationship
    runs the opposite direction (this template references its base via inherit_id), so checking
    "does anything reference user_navbar_inherit_logbook" was always going to come up empty for
    every single one of these, regardless of whether the feature is real and live."""
    declarations = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                for match in TEMPLATE_ID_RE.finditer(line):
                    if "inherit_id" in line:
                        continue
                    declarations.append((module, match.group(1), path, lineno))
    except (UnicodeDecodeError, OSError):
        pass
    return declarations


def _find_view_declarations(path, module):
    """<record id=X model="ir.ui.view"> only -- the id and model attributes can appear in
    either order and possibly on different lines within the same open tag, so this scans the
    whole record block, not a single line, unlike the simpler single-line template regex. Same
    inherit_id exclusion as _find_template_declarations() above, but checked across the whole
    record block (not just the opening tag), since a view's inherit_id is conventionally a
    separate <field name="inherit_id" .../> line within the record, not an attribute on
    <record> itself."""
    declarations = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except (UnicodeDecodeError, OSError):
        return declarations

    record_blocks = list(re.finditer(r"<record\b[^>]*>.*?</record>", content, re.DOTALL))
    for match in record_blocks:
        block = match.group(0)
        id_match = re.search(r'\bid="([^"]+)"', block)
        model_match = re.search(r'\bmodel="ir\.ui\.view"', block)
        if not (id_match and model_match):
            continue
        if re.search(r'<field\s+name="inherit_id"', block):
            continue
        lineno = content[: match.start()].count("\n") + 1
        declarations.append((module, id_match.group(1), path, lineno))
    return declarations


def _find_js_files(module_dir, module):
    declarations = []
    js_dir = os.path.join(module_dir, "static", "src", "js")
    if not os.path.isdir(js_dir):
        return declarations
    for root, dirs, files in os.walk(js_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for filename in files:
            if not filename.endswith(".js") or filename.endswith(".test.js"):
                continue
            declarations.append((module, filename, os.path.join(root, filename), 1))
    return declarations


TOKEN_RE = re.compile(r"[\w.]+")


def _build_reference_index(repo_roots):
    """A single O(corpus size) pass, not O(declarations x corpus size): the first, naive
    implementation re-scanned the whole corpus once per declared id/filename with its own fresh
    regex search, which took minutes and was still running uncompleted against the real ~1664
    file repo after 2+ minutes -- killed and replaced with this inverted-index approach.
    Tokenizes every line once into word-like tokens (identifiers, dotted qualified refs like
    'mod_a.live_page', and filenames like 'live.js' all come out as single tokens since '.'
    counts as a word character here), and records every (path, lineno) each token occurs at.
    A "does X appear anywhere else" question is then a single dict lookup instead of a fresh
    scan, independent of how many declarations are being checked."""
    index = {}
    for repo_root in repo_roots:
        for path in _iter_all_files(repo_root, (".py", ".xml", ".js")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except (UnicodeDecodeError, OSError):
                continue
            for lineno, line in enumerate(lines, 1):
                for token in TOKEN_RE.findall(line):
                    index.setdefault(token, []).append((path, lineno))
    return index


def _references(needle, reference_index, own_path, own_lineno):
    """Generous, textual reachability signal: does `needle` (a bare id or a bare filename)
    appear anywhere in the scanned corpus other than on its own declaring line? Deliberately not
    scoped to a specific XML attribute or Python call shape -- see the module docstring for why
    a loose, generous match is the intentional, safer failure direction for an advisory (not
    yet gating) check. Exact-token match (via the tokenized index), not plain substring: a real
    false negative was found live while building this checker -- 'elmer_directory' (the real
    dead template's own id) is a plain substring of 'test_elmer_directory' (its own
    audit-ignore-view anchor comment, a few lines below the declaration), so a bare `needle in
    line` check treated the id as "referenced" by its own anchor comment and never flagged the
    real dead file this checker exists to catch. Token-exact matching doesn't have this problem:
    'test_elmer_directory' tokenizes to one token, distinct from 'elmer_directory'."""
    for path, lineno in reference_index.get(needle, ()):
        if path == own_path and lineno == own_lineno:
            continue
        return True
    return False


def check_dead_code(repo_roots):
    dead_templates = []
    dead_views = []
    dead_js = []

    reference_index = _build_reference_index(repo_roots)

    for repo_root in repo_roots:
        for module, module_dir in _iter_module_dirs(repo_root):
            views_dir = os.path.join(module_dir, "views")
            if os.path.isdir(views_dir):
                for path in _iter_all_files(views_dir, (".xml",)):
                    for mod, tid, tpath, lineno in _find_template_declarations(path, module):
                        qualified = f"{mod}.{tid}"
                        if not _references(
                            tid, reference_index, tpath, lineno
                        ) and not _references(qualified, reference_index, tpath, lineno):
                            dead_templates.append((tpath, lineno, tid))
                    for mod, vid, vpath, lineno in _find_view_declarations(path, module):
                        qualified = f"{mod}.{vid}"
                        if not _references(
                            vid, reference_index, vpath, lineno
                        ) and not _references(qualified, reference_index, vpath, lineno):
                            dead_views.append((vpath, lineno, vid))

            for mod, filename, jpath, lineno in _find_js_files(module_dir, module):
                if not _references(filename, reference_index, jpath, lineno):
                    dead_js.append((jpath, lineno, filename))

    return dead_templates, dead_views, dead_js


def main():
    if len(sys.argv) < 2:
        print("Usage: check_dead_code.py <repo_root>")
        sys.exit(1)

    repo_roots = _resolve_repo_roots(sys.argv[1])
    dead_templates, dead_views, dead_js = check_dead_code(repo_roots)

    has_findings = bool(dead_templates or dead_views or dead_js)

    if dead_templates:
        print("⚠️  Website templates with no reference anywhere else in the repo (possibly dead):")
        for path, lineno, tid in dead_templates:
            print(f"  - {path}:{lineno} <template id=\"{tid}\">")
            print(
                f"      [!] DIAGNOSTIC FOR AI: No controller/menu/JS reference to '{tid}' found "
                f"anywhere. Confirm no route renders it before treating as dead -- if genuinely "
                f"unreachable, this needs a human decision to delete or wire up, not an "
                f"unattended fix (see AUDIT_IGNORE_VIEW_UNVERIFIED_CLAIMS.md for precedent)."
            )

    if dead_views:
        print("⚠️  Backend ir.ui.view records with no reference anywhere else in the repo (possibly dead):")
        for path, lineno, vid in dead_views:
            print(f"  - {path}:{lineno} <record id=\"{vid}\" model=\"ir.ui.view\">")
            print(
                f"      [!] DIAGNOSTIC FOR AI: No action/inherit_id/env.ref() reference to "
                f"'{vid}' found anywhere. Confirm no action or dynamic client-action dict opens "
                f"it before treating as dead."
            )

    if dead_js:
        print("⚠️  JS files with no reference anywhere else in the repo (possibly dead):")
        for path, lineno, filename in dead_js:
            print(f"  - {path}")
            print(
                f"      [!] DIAGNOSTIC FOR AI: '{filename}' is not in any __manifest__.py asset "
                f"bundle, not imported by another JS file, and not referenced as a literal "
                f"static-URL string (e.g. audioWorklet.addModule()) anywhere."
            )

    if not has_findings:
        print("✅ No unreferenced website templates, backend views, or JS files found.")

    # Advisory only -- not yet a hard CI gate. See module docstring: a brand-new heuristic
    # checker needs its real-repo false-positive rate established first.
    sys.exit(0)


if __name__ == "__main__":
    main()
