#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
External Library Locality Linter
---------------------------------
The "external" module exists to be the single, canonical place third-party
vendored libraries live in this codebase ("Local hosting of external
libraries for isolated networks" is its own manifest summary). Vendoring
the same library a second time in another module's own static/lib/ or
static/.../node_modules/ is exactly how this codebase ended up with two
independently-drifted copies of transformers.js (2.16.0 in ham_events,
2.16.1 in external) -- silently different versions of the same NLP
pipeline, discovered only by accident while root-causing an unrelated
minifier bug (hams_com commit f1f00511). It also multiplies exposure to
that same bug class: every vendored copy is a separate place rjsmin can
corrupt on a nested template literal.

This flags any .js or .css file sitting in <module>/static/**/lib/ or
<module>/static/**/node_modules/ for any module other than "external"
itself -- this codebase's own established vendoring convention (see
ham_shack, ham_satellite, external's own directory layouts) uses those
two directory names exclusively for genuine third-party code, never
first-party utilities, so the false-positive rate is low. Non-JS/CSS
files (data files, one-off patch scripts) in the same directories are
not flagged; they're not "a library".
"""

import os
import sys

# Files already vendored outside "external" that are NOT yet migrated,
# each because moving them safely needs something this linter can't
# verify by itself:
EXCLUDED_FILES = {
    # ham_satellite's three.js/OrbitControls.js have undocumented,
    # non-obvious history around Odoo's "/** @odoo-module **/" pragma
    # (see fix_pragma.py / fix_three.py in the same directory -- one adds
    # the pragma to three.min.js and satellite.min.js, the other strips
    # it back out of OrbitControls.js only). That toggling almost
    # certainly encodes a real constraint on how Odoo's js_transpiler
    # treats each file, but why is not written down anywhere, and the 3D
    # satellite-tracking view that depends on it needs a live
    # WebGL-capable browser to re-verify after any change -- not
    # available in an unattended session. Left in place rather than
    # guessed at; migrating these needs the module's own author to
    # confirm the pragma requirement and re-test the view live.
    "ham_satellite/static/src/lib/OrbitControls.js",
    "ham_satellite/static/src/lib/satellite.min.js",
    "ham_satellite/static/src/lib/three.min.js",
}


def _resolve_repo_root(given_path):
    """run_linters.py's own `dir_path` resolves to the hams_shared directory itself, not a real
    repo root (same bug found and fixed in check_model_extension_collisions.py and others).
    Unlike those, this checker's own zero-violations result happened to be correct by coincidence
    either way (hams_shared genuinely has no vendored-library files, so scanning it never
    produced a false negative) -- but it was still scanning the wrong, much smaller tree (2684
    files under hams_shared vs. 11210 under a real repo root), so a real future violation in
    hams_com or hams_open could have gone undetected. Detect the hams_shared case by name and
    redirect to its real parent repo, same as the other fixed checkers."""
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        return os.path.dirname(given_path)
    return given_path


def find_violations(repo_root):
    violations = []
    for root, dirs, files in os.walk(repo_root):
        if "radae" in dirs:
            dirs.remove("radae")
        dirs[:] = [d for d in dirs if d not in ("__pycache__",) and not d.startswith(".")]

        rel_root = os.path.relpath(root, repo_root)
        parts = rel_root.split(os.sep)
        if not parts or parts[0] in (".", "external"):
            continue
        if "static" not in parts:
            continue
        if "lib" not in parts and "node_modules" not in parts:
            continue

        for f in files:
            if not (f.endswith(".js") or f.endswith(".css")):
                continue
            rel_path = os.path.join(rel_root, f)
            if rel_path in EXCLUDED_FILES:
                continue
            violations.append(rel_path)
    return violations


def main():
    if len(sys.argv) < 2:
        print("Usage: check_external_library_locality.py <repo_root>")
        sys.exit(1)

    repo_root = _resolve_repo_root(sys.argv[1])
    violations = find_violations(repo_root)

    if violations:
        print("🚨 VENDORED LIBRARY OUTSIDE \"external\" MODULE")
        for v in sorted(violations):
            print(f"  {v}")
        print(
            "  Fix: move the file into external/static/src/node_modules/<library>/\n"
            "  and update whatever manifest/template referenced the old path.\n"
            "  The \"external\" module is this codebase's single place to host\n"
            "  vendored third-party libraries -- see ham_shack's d3/topojson\n"
            "  entries for a worked example (hams_com ham_shack/__manifest__.py)."
        )
        sys.exit(1)

    print("[+] External Library Locality Linter: no vendored libraries outside \"external\".")
    sys.exit(0)


if __name__ == "__main__":
    main()
