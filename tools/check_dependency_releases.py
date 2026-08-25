#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Dependency release watch
-------------------------------------------------------------------------------------------
Reads hams_shared/tools/dependency_watch.json -- the codebase's own list of external,
non-package-manager dependencies it tracks a pinned version or commit of (ardopcf, mercury,
pat, hamlib, plus the three entries already in binary_manifest.json) -- and checks each one
against its real upstream GitHub repo: 'release' mode compares against the latest published
release tag, 'branch' mode compares against the tracked branch's current HEAD commit.

This is deliberately separate from cargo-deny/pip-audit (RELAY_SUPPLY_CHAIN_SECURITY.md
section 3): those catch known *vulnerabilities* in package-manager-resolved dependencies.
This catches plain staleness in dependencies we build from source or vendor by hand, which
those tools have no visibility into at all. Exits non-zero if anything is stale, so it can be
wired into CI (a scheduled workflow, not a per-push gate -- upstream releases don't happen on
our schedule) the same way the other scanners are.

Requires network access and (for higher, more reliable rate limits) an optional GITHUB_TOKEN
in the environment -- unauthenticated GitHub API calls are limited to 60/hour per source IP,
which this script's small dependency count stays well under, but a token avoids ever caring.
"""

import json
import os
import sys
import urllib.error
import urllib.request

GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT_SECONDS = 15


def _github_get(path):
    req = urllib.request.Request(f"{GITHUB_API}{path}")
    req.add_header("Accept", "application/vnd.github+json")
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        return json.load(resp)


def check_release_mode(repo, pinned):
    data = _github_get(f"/repos/{repo}/releases/latest")
    latest_tag = data["tag_name"]
    return latest_tag, latest_tag != pinned


def check_branch_mode(repo, branch, pinned):
    data = _github_get(f"/repos/{repo}/commits/{branch}")
    latest_sha = data["sha"]
    return latest_sha, latest_sha != pinned


def check_one(name, entry):
    repo = entry["repo"]
    mode = entry["mode"]
    pinned = entry["pinned"]
    try:
        if mode == "release":
            latest, stale = check_release_mode(repo, pinned)
        elif mode == "branch":
            latest, stale = check_branch_mode(repo, entry["branch"], pinned)
        else:
            return {"name": name, "error": f"unknown mode '{mode}'"}
    except urllib.error.HTTPError as e:
        return {"name": name, "error": f"HTTP {e.code} from GitHub API for {repo}"}
    except urllib.error.URLError as e:
        return {"name": name, "error": f"network error reaching GitHub API: {e.reason}"}

    return {"name": name, "repo": repo, "pinned": pinned, "latest": latest, "stale": stale}


def main():
    manifest_path = os.path.join(os.path.dirname(__file__), "dependency_watch.json")
    if len(sys.argv) > 1:
        manifest_path = sys.argv[1]

    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    entries = {k: v for k, v in manifest.items() if not k.startswith("_")}
    results = [check_one(name, entry) for name, entry in sorted(entries.items())]

    any_stale = False
    any_error = False
    for r in results:
        if "error" in r:
            any_error = True
            print(f"⚠️  {r['name']}: could not check -- {r['error']}")
        elif r["stale"]:
            any_stale = True
            print(f"🔄 {r['name']} ({r['repo']}): pinned {r['pinned']!r}, latest is {r['latest']!r}")
        else:
            print(f"✅ {r['name']} ({r['repo']}): up to date at {r['pinned']!r}")

    if any_stale:
        print("\nOne or more dependencies have a newer upstream release/commit than what's pinned.")
    sys.exit(1 if (any_stale or any_error) else 0)


if __name__ == "__main__":
    main()
