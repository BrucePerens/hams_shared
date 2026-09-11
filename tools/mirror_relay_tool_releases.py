#!/usr/bin/env python3
# Copyright © Bruce Perens K6BP. All Rights Reserved. This software is proprietary and confidential.

"""Mirrors a specific, pinned upstream release of an optional hams_local_relay tool (pat,
Direwolf) onto hams.com, via /api/relay_bridge/tool/publish (tool_publish_api.py).

Deliberately maintainer-run, not CI-scheduled: pat/Direwolf release cadence is slow (pat's
last tagged release as of this writing is v1.0.0), so a scheduled job re-checking on every
push -- or even daily -- would be pure overhead for something that changes rarely. Run this by
hand when a new upstream version is worth mirroring, after reviewing what changed.

Deliberately pins an exact release tag per invocation rather than always fetching "latest" --
what relay_tool_release.py stores is a promise about what an operator's "Download and install"
click actually gets; if this silently tracked upstream's own "latest" release, that promise
would drift out from under this project's own review with no PR to catch it.

Looks up the real asset list for the given tag via GitHub's own public releases API (not a
hand-built download URL): confirmed directly, per-project asset filenames vary in ways a single
template can't predict (Direwolf's own release assets embed a build-specific git-hash suffix,
e.g. "direwolf-1.8.1-a231971_x86_64.zip", not derivable from the tag "1.8.1" alone) -- the
per-platform patterns below match against whatever the real asset list contains instead.

Usage:
    python3 tools/mirror_relay_tool_releases.py --tool pat --tag v1.0.0 \\
        --odoo-url https://hams.com --publish-key "$(cat ~/.secrets/hams_relay_tool_publish_key)"

    python3 tools/mirror_relay_tool_releases.py --tool direwolf --tag 1.8.1 \\
        --odoo-url https://hams.com --publish-key "..."

Requires network access to api.github.com/github.com and to --odoo-url. Requires the same
publish key CI uses for /api/relay_bridge/binary/publish and /api/relay_bridge/source/publish
(this is the same publish service account/secret, not a new one -- see tool_publish_api.py's
own doc comment).
"""

import argparse
import base64
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request

# One entry per (tool, platform) this project actually supports mirroring, per
# relay_tool_release.py's own documented reasoning: pat has real upstream release assets for
# every platform; Direwolf only ships one, Windows-only (Linux/macOS users are expected to use
# apt/Homebrew, already covered by this project's own install_relay_runtime_deps.sh and
# packaging -- not this mechanism). Each value is a regex matched against the real asset
# filenames GitHub's own release API returns for the given tag -- confirmed directly against a
# real `gh api repos/<owner>/<repo>/releases/latest` call for each project before writing
# these, not guessed.
GITHUB_REPO = {"pat": "la5nta/pat", "direwolf": "wb2osz/direwolf"}
ASSET_PATTERNS = {
    ("pat", "windows"): re.compile(r"^pat_.*_windows_i386\.zip$"),
    ("pat", "macos"): re.compile(r"^pat_.*_darwin_amd64\.pkg$"),
    ("pat", "linux"): re.compile(r"^pat_.*_linux_amd64\.tar\.gz$"),
    ("direwolf", "windows"): re.compile(r"^direwolf-.*_x86_64\.zip$"),
}


def fetch(url: str, accept_json: bool = False) -> bytes:
    headers = {"Accept": "application/vnd.github+json"} if accept_json else {}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def find_release_assets(tool: str, tag: str) -> dict:
    """Returns {filename: browser_download_url} for the given tag's real release."""
    repo = GITHUB_REPO[tool]
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    try:
        data = json.loads(fetch(url, accept_json=True))
    except urllib.error.HTTPError as e:
        raise SystemExit(
            f"Failed to look up {repo}'s release tag '{tag}': HTTP {e.code} -- "
            "check the tag actually exists (see the project's own GitHub Releases page)"
        ) from e
    return {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}


def publish(odoo_url: str, publish_key: str, tool: str, platform: str, tag: str,
            binary_bytes: bytes, filename: str, source_url: str) -> None:
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "api_key": publish_key,
            "tool": tool,
            "platform": platform,
            "tool_version": tag,
            "binary_base64": base64.b64encode(binary_bytes).decode("ascii"),
            "filename": filename,
            "source_url": source_url,
        },
    }
    req = urllib.request.Request(
        f"{odoo_url}/api/relay_bridge/tool/publish",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # Real bug found 2026-09-10: find_release_assets() above already wraps its own real network
    # call's HTTPError into a clear, actionable SystemExit (a bad tag name, a renamed repo), but
    # this publish call -- an equally real, equally likely failure point (a wrong/expired
    # --publish-key, the endpoint temporarily down, a server-side 500) -- had no such handling at
    # all, so any non-2xx response here crashed with a raw, unhandled urllib traceback instead of
    # the same clear "Publish failed for X/Y: ..." message this file's own error-reporting style
    # already establishes for the sibling failure case one function up.
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise SystemExit(
            f"Publish failed for {tool}/{platform}: HTTP {e.code} from {odoo_url} -- {body}"
        ) from e
    status = result.get("result", {})
    print(f"  {tool}/{platform}: {status}")
    if status.get("status") != "success":
        raise SystemExit(f"Publish failed for {tool}/{platform}: {status}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--tool", required=True, choices=sorted(GITHUB_REPO))
    parser.add_argument("--tag", required=True,
                         help="The upstream project's own release tag exactly as it names it "
                              "(pat's tags have a leading 'v', e.g. 'v1.0.0'; Direwolf's don't, "
                              "e.g. '1.8.1') -- check the project's own GitHub Releases page.")
    parser.add_argument("--odoo-url", required=True)
    parser.add_argument("--publish-key", required=True)
    args = parser.parse_args()

    platforms = [p for (t, p) in ASSET_PATTERNS if t == args.tool]
    print(f"Looking up {args.tool} release '{args.tag}' assets...")
    assets = find_release_assets(args.tool, args.tag)
    print(f"  found {len(assets)} asset(s): {', '.join(sorted(assets))}")

    for platform in platforms:
        pattern = ASSET_PATTERNS[(args.tool, platform)]
        matches = [name for name in assets if pattern.match(name)]
        if not matches:
            raise SystemExit(
                f"No asset in tag '{args.tag}' matches the expected {args.tool}/{platform} "
                f"pattern ({pattern.pattern}) -- upstream's own naming may have changed; "
                "update ASSET_PATTERNS after checking their real release page."
            )
        if len(matches) > 1:
            raise SystemExit(
                f"Multiple assets in tag '{args.tag}' match the {args.tool}/{platform} "
                f"pattern: {matches} -- ambiguous, refusing to guess."
            )
        filename = matches[0]
        url = assets[filename]
        print(f"Fetching {url} ...")
        binary_bytes = fetch(url)
        sha256 = hashlib.sha256(binary_bytes).hexdigest()
        print(f"  {len(binary_bytes)} bytes, sha256={sha256}")
        publish(args.odoo_url, args.publish_key, args.tool, platform, args.tag,
                binary_bytes, filename, url)

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
