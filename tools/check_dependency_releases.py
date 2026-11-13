#!/usr/bin/env python3
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

import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import check_target


# [@ANCHOR: check_dependency_releases:check_dependency_releases__same_host_redirect_class]
class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to the same https host. GitHub answers a renamed repository with a 301 to
    `api.github.com/repositories/<id>`; anywhere else the token must not follow (urllib copies every request header to the
    target of a redirect), so the redirect is refused and surfaces as the HTTPError of the 3xx status."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if new.scheme != "https" or new.netloc.lower() != old.netloc.lower():
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SameHostRedirect)

GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT_SECONDS = 15

# A hold may not be set further ahead than this, so it cannot silently become permanent.
# [@ANCHOR: dependency_watch_hold_marker]
MAX_HOLD_DAYS = 90


def _github_get(path):
    req = urllib.request.Request(f"{GITHUB_API}{path}")
    req.add_header("Accept", "application/vnd.github+json")
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with _OPENER.open(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        return json.load(resp)


def check_release_mode(repo, pinned):
    data = _github_get(f"/repos/{repo}/releases/latest")
    latest_tag = data["tag_name"]
    return latest_tag, latest_tag != pinned


def check_branch_mode(repo, branch, pinned):
    data = _github_get(f"/repos/{repo}/commits/{branch}")
    latest_sha = data["sha"]
    return latest_sha, latest_sha != pinned


# [@ANCHOR: dependency_hold_status]
def hold_status(entry, today):
    """Classifies an entry's optional "hold" marker (see tools/README.md, "Held pins").

    Returns None when the entry has no hold, ("active", "<reason> (until <date>)") when it
    is valid and unexpired, ("expired", "<message>") when its date has passed, and
    ("invalid", "<message>") when it is malformed (not an object, empty reason, missing or
    unparseable "until", or "until" more than MAX_HOLD_DAYS from today).
    """
    if "hold" not in entry:
        return None
    hold = entry["hold"]
    if not isinstance(hold, dict):
        return ("invalid", "hold must be an object with 'reason' and 'until'")
    reason = hold.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return ("invalid", "hold has no reason")
    try:
        until = datetime.date.fromisoformat(str(hold.get("until")))
    except ValueError:
        return ("invalid", "hold 'until' must be an ISO date (YYYY-MM-DD)")
    if until > today + datetime.timedelta(days=MAX_HOLD_DAYS):
        return ("invalid", f"hold 'until' {until} is more than {MAX_HOLD_DAYS} days away")
    if until < today:
        return ("expired", f"hold expired on {until}: {reason.strip()}")
    return ("active", f"{reason.strip()} (until {until})")


def check_one(name, entry, today=None):
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
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        # A response GitHub's own API returns with HTTP 200 but
        # a body this script doesn't expect -- a non-JSON error/maintenance page (json.load
        # raises JSONDecodeError), or valid JSON missing "tag_name"/"sha" (a schema a future
        # GitHub API change, or an edge-case repo state, could produce) -- must be caught,
        # otherwise ONE malformed entry's response crashes `check_one()` with an uncaught
        # exception. Since `main()` calls `check_one()` for every tracked dependency inside a
        # single list comprehension with no per-item isolation, that uncaught exception
        # would abort the ENTIRE scheduled run before it could report on ANY of the other,
        # perfectly healthy dependencies -- the opposite of this checker's own "isolate one
        # entry's failure from the rest" design already proven for HTTPError/URLError above.
        return {"name": name, "error": f"unexpected response from GitHub API for {repo}: {e}"}

    result = {"name": name, "repo": repo, "pinned": pinned, "latest": latest, "stale": stale}
    hold = hold_status(entry, today or datetime.date.today())
    if hold is not None:
        state, message = hold
        if state == "invalid":
            return {"name": name, "error": message}
        if stale and state == "active":
            result["stale"] = False
            result["held"] = message
        elif stale:
            result["hold_note"] = message
        else:
            result["hold_note"] = f"hold not needed, the pin is current ({message})"
    return result


def main():
    manifest_path = os.path.join(os.path.dirname(__file__), "dependency_watch.json")
    if len(sys.argv) > 1:
        manifest_path = sys.argv[1]
    manifest_path = check_target.require_file(manifest_path, "check_dependency_releases")

    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    entries = {k: v for k, v in manifest.items() if not k.startswith("_")}
    check_target.require_scanned(len(entries), manifest_path, "check_dependency_releases", "dependency entries")
    results = [check_one(name, entry) for name, entry in sorted(entries.items())]

    any_stale = False
    any_error = False
    for r in results:
        if "error" in r:
            any_error = True
            print(f"⚠️  {r['name']}: could not check -- {r['error']}")
        elif r.get("held"):
            print(f"⏸  {r['name']} ({r['repo']}): pinned {r['pinned']!r}, latest is {r['latest']!r}; HELD: {r['held']}")
        elif r["stale"]:
            any_stale = True
            note = f" ({r['hold_note']})" if r.get("hold_note") else ""
            print(f"🔄 {r['name']} ({r['repo']}): pinned {r['pinned']!r}, latest is {r['latest']!r}{note}")
        else:
            print(f"✅ {r['name']} ({r['repo']}): up to date at {r['pinned']!r}")

    if any_stale:
        print("\nOne or more dependencies have a newer upstream release/commit than what's pinned.")
    sys.exit(1 if (any_stale or any_error) else 0)


if __name__ == "__main__":
    main()
