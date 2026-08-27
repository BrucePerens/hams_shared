#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Checks all source files for hard-coded absolute paths based in /home.

.mypy_cache is excluded: its .json files legitimately embed the host's
real absolute paths as cached type-check data, not source code, and it
self-ignores via mypy's own generated .mypy_cache/.gitignore -- flagging
it here is a false positive, not a real finding.

archive/ is excluded for the same reason: it holds frozen, historical
snapshots of past pipeline runs (e.g. ingestion metadata), not live
source -- rewriting a real developer path baked into a historical
artifact to "fix" this check would make the archive inaccurate, not
correct. .claude/ is excluded too: it's this tool's own local
configuration (skills, agent definitions, settings), not shipped
application source, and a skill file legitimately documents a real,
box-specific path (e.g. where a secret file lives on this dev box) as
its actual content, not a mistake to flag.

ics_training/ is excluded for the same reason as archive/, one stage
earlier: it's the live, in-progress working directory of the ICS
ingestion pipeline (confirmed by the total absence of a
__manifest__.py anywhere under it -- it's not an installed Odoo
module), holding dedup-tracking data (used_names.csv,
used_disasters.csv) and in-progress course JSON that legitimately
embeds real local staging paths (e.g. "reference_sheets_used") as
internal provenance metadata, not shipped, user-facing content. Once
a run completes it's snapshotted into archive/ under the identical
naming convention (e.g. course_ICS_20260814_194206.json here becomes
archive/ics_ingestion_20260814_194206/ -- same timestamp, same
pipeline, just the next stage of the same lifecycle archive/ already
covers).

ham_auxcomm_training/ is excluded for the same reason as ics_training/
(no __manifest__.py anywhere under it either -- confirmed, not
assumed): it holds a single review_draft.html, a human-review preview
artifact from the same ingestion pipeline family, whose rendered
<img src> can legitimately point at a real local path (e.g. a Gemini
cache directory) since it's a draft never meant to be served.

course_*.json files are skipped by name (not by directory) wherever
they appear, including inside REAL Odoo modules like ham_training/ --
confirmed directly that ham_training/data/course_HAM_TECH.json is not
referenced anywhere in ham_training/__manifest__.py's own data list,
unlike every file actually shipped from that same data/ directory, so
it's the identical pipeline-provenance artifact as ics_training/'s
course_ICS.json, just generated into a real module's directory instead
of a staging-only one. A directory-level exclusion doesn't fit here --
ham_training/ is a real module and its other files must stay checked.
"""

import os
import sys


def check_absolute_paths(repo_dir):
    violations = []

    # Common directories to ignore
    ignore_dirs = {
        ".git",
        "node_modules",
        "venv",
        "env",
        ".venv",
        "__pycache__",
        ".agents",
        "target",
        "radae",
        "agents",
        "scratch",
        ".mypy_cache",
        "archive",
        ".claude",
        "ics_training",
        "ham_auxcomm_training",
    }
    # Only check text-based files
    valid_exts = {
        ".py",
        ".js",
        ".xml",
        ".csv",
        ".md",
        ".json",
        ".txt",
        ".sh",
        ".html",
        ".css",
    }

    for root, dirs, files in os.walk(repo_dir):
        # Modify dirs in-place to ignore specified directories
        dirs[:] = [d for d in dirs if d not in ignore_dirs]

        for file in files:
            ext = os.path.splitext(file)[1]
            if ext not in valid_exts and file != "Makefile":
                continue

            # Ingestion-pipeline course data, wherever it lands (including
            # inside a real Odoo module's data/ directory, e.g.
            # ham_training/data/course_HAM_TECH.json) -- same rationale as
            # ics_training/'s directory-level exclusion above, applied by
            # filename since the enclosing directory here is real source.
            if file.startswith("course_") and file.endswith(".json"):
                continue

            file_path = os.path.join(root, file)
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if "/h" + "ome/" in line:
                            # Skip if it is a file URI which may be in SKILL.md examples or similar
                            # Actually, we should even prohibit the h-o-m-e path because it's non-portable
                            violations.append(
                                f"{os.path.relpath(file_path, repo_dir)}:{i} Contains hardcoded home path"
                            )
            except UnicodeDecodeError:
                # Skip binary files or files with weird encodings
                pass

    return violations


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: check_absolute_paths.py <repo_dir>")
        sys.exit(1)

    repo_dir = sys.argv[1]
    violations = check_absolute_paths(repo_dir)

    if violations:
        print("❌ Absolute Paths Violations:")
        for v in violations:
            print(f"  - {v}")
        sys.exit(1)

    sys.exit(0)
