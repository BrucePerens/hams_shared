#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
ADR-0102: night-watch's fast merge path for its own trusted, autonomous work.

Finds open PRs whose head branch starts with "night-shift/" on hams_com and hams_shared, and
approves + merges them -- using a credential dedicated to this one job, distinct from whatever
identity opened the PR (GitHub refuses to let a PR's own author approve it, so this separation
is structural, not just caution). Deliberately excludes anything else by construction: a PR
from any other branch (in particular hams_helpdesk's "ai-triage/" ticket-triage PRs) is never
matched, never touched, and always falls through to a real human review.

Deliberately does NOT pre-check required status checks itself before attempting a merge: doing
that needs a "Checks" read permission on top of what this PAT already has (confirmed directly --
`statusCheckRollup` came back "Resource not accessible by personal access token" with only
Pull requests + Contents scopes), and would just be a second, independently-maintained copy of
a decision GitHub's own branch protection already makes authoritatively. Simpler and more
correct to always attempt the merge and let GitHub refuse it when checks haven't passed yet --
this script just tries again next cycle.

Run once per night-watch hourly cycle (see that skill's own SKILL.md), not as a persistent
daemon or a GitHub Actions workflow (standing policy: AI agents do not run in GitHub Actions).

Credential: ~/.secrets/hams_com_ci/NIGHT_WATCH_REVIEWER_GITHUB_PAT -- this project's real
secret convention (see ADR-0102's own discussion of why this is not in AWS Secrets Manager).
Fine-grained PAT, repo-scoped to hams_com and hams_shared only: Pull requests (read/write) and
Contents (read/write, required by GitHub's own merge endpoint even though this script never
pushes code), no Administration. Must never be readable by the hams_helpdesk ticket-triage
service -- that daemon's own PAT is a separate secret with disjoint access.
"""
import json
import logging
import os
import subprocess
import sys

_logger = logging.getLogger("night_watch_review_and_merge")

_BRANCH_PREFIX = "night-shift/"
_REPOS = ["BrucePerens/hams_com", "BrucePerens/hams_shared"]
_PAT_PATH = os.path.expanduser("~/.secrets/hams_com_ci/NIGHT_WATCH_REVIEWER_GITHUB_PAT")


def _read_reviewer_token():
    with open(_PAT_PATH, "r", encoding="ascii") as f:
        return f.read().strip()


def _gh(*args, token, check=True):
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    return subprocess.run(
        ["gh", *args], env=env, capture_output=True, text=True, timeout=60, check=check
    )


def _open_night_shift_prs(repo, token):
    result = _gh(
        "pr", "list", "--repo", repo, "--state", "open",
        "--json", "number,headRefName",
        token=token,
    )
    return [
        pr for pr in json.loads(result.stdout)
        if pr["headRefName"].startswith(_BRANCH_PREFIX)
    ]


def _review_and_merge(repo, pr, token):
    number = pr["number"]
    _gh(
        "pr", "review", str(number), "--repo", repo, "--approve",
        "--body", "Auto-approved by night-watch's review/merge script (ADR-0102): "
                  "night-shift/* branch.",
        token=token,
    )
    merge = _gh(
        "pr", "merge", str(number), "--repo", repo, "--squash", "--delete-branch",
        token=token, check=False,
    )
    if merge.returncode == 0:
        _logger.info("Merged %s#%d (%s)", repo, number, pr["headRefName"])
    else:
        # Most commonly: required status checks haven't passed yet. Not an error -- the
        # approval above still stands, and this PR is picked up again next cycle.
        _logger.info(
            "%s#%d (%s) approved but not yet mergeable: %s",
            repo, number, pr["headRefName"], merge.stderr.strip(),
        )


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = _read_reviewer_token()

    for repo in _REPOS:
        for pr in _open_night_shift_prs(repo, token):
            _review_and_merge(repo, pr, token)
    return 0  # "nothing to merge yet" is not a failure


if __name__ == "__main__":
    sys.exit(main())
