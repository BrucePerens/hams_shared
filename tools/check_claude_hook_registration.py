#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Every repository checkout registers the signal guard hook (Bruce, 2026-11-09).

On 2026-11-09 an agent ran `kill $pp` on its own parent (the `systemd --user` that started it) because the PreToolUse hook
`hams_shared/tools/claude_hooks/guard_signals.py` was not active: it was registered only in the `.claude/settings.json` of
`hams_com` and `hams_open`, and the agent worked in a `hams_shared` worktree, which had none. Claude Code reads the project
settings of the directory it runs in, so each of the three checkouts (hams_com, hams_open, hams_shared) needs its own tracked
`.claude/settings.json` with the hook, and a worktree of any of them (a checkout of the same tree) then has it too.

    check_claude_hook_registration.py [REPO_DIR ...]      exit 0 when every REPO_DIR registers the hook, 1 otherwise

`REPO_DIR` defaults to the checkout this script lives in (the parent of `tools/`). A repository passes when
`.claude/settings.json` exists, is tracked by git (when `REPO_DIR` is inside a git work tree), is valid JSON, and its
`hooks.PreToolUse` has an entry whose `matcher` matches `Bash` and which holds a `command` hook that runs
`claude_hooks/guard_signals.py` by a path that does not depend on the current directory (`$HOME/...`, `$CLAUDE_PROJECT_DIR/...`
or an absolute path). Documentation: tools/README.md "Guard hook registration check".
"""

import json
import os
import re
import subprocess
import sys

import check_target

HOOK_SCRIPT = "claude_hooks/guard_signals.py"
SETTINGS = os.path.join(".claude", "settings.json")
_ANCHORED_PATH_RE = re.compile(r"""(?<![\w./~$-])(?:\$HOME|\$\{HOME\}|\$CLAUDE_PROJECT_DIR|\$\{CLAUDE_PROJECT_DIR\}|~|/)[^\s"';]*""" + re.escape(HOOK_SCRIPT))


# [@ANCHOR: check_claude_hook_registration:command_is_guard]
def command_is_guard(command):
    """True when the hook `command` text runs `claude_hooks/guard_signals.py` by a path that is anchored (`$HOME`, `~`,
    `$CLAUDE_PROJECT_DIR` or absolute), so it works from a worktree and from any directory."""
    return isinstance(command, str) and _ANCHORED_PATH_RE.search(command) is not None


# [@ANCHOR: check_claude_hook_registration:matcher_covers_bash]
def matcher_covers_bash(matcher):
    """True when a PreToolUse `matcher` (a regex; empty or `*` means every tool) applies to the Bash tool."""
    if matcher in (None, "", "*"):
        return True
    try:
        return re.fullmatch(matcher, "Bash") is not None
    except re.error:
        return False


# [@ANCHOR: check_claude_hook_registration:is_tracked]
def is_tracked(repo_dir):
    """False only when `repo_dir` is in a git work tree and `.claude/settings.json` is not tracked by it (a file that exists on
    disk but is not committed does not protect a fresh checkout or a worktree). True outside git or when git cannot be run."""
    try:
        inside = subprocess.run(["git", "-C", repo_dir, "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True,
                                timeout=60)
        if inside.returncode != 0:
            return True
        done = subprocess.run(["git", "-C", repo_dir, "ls-files", "--error-unmatch", "--", SETTINGS], capture_output=True,
                              text=True, timeout=60)
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True


# [@ANCHOR: check_claude_hook_registration:check_repo]
def check_repo(repo_dir):
    """The problems with the guard registration of the checkout `repo_dir`, as a list of sentences (empty: it is registered)."""
    path = os.path.join(repo_dir, SETTINGS)
    name = os.path.basename(os.path.abspath(repo_dir))
    if not os.path.isfile(path):
        return [f"{name}: {SETTINGS} does not exist; register the PreToolUse Bash hook {HOOK_SCRIPT} in it"]
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        return [f"{name}: {SETTINGS} cannot be read as JSON: {error}"]
    entries = ((data.get("hooks") or {}).get("PreToolUse") or []) if isinstance(data, dict) else []
    found = False
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not matcher_covers_bash(entry.get("matcher")):
            continue
        for hook in entry.get("hooks") or []:
            if isinstance(hook, dict) and hook.get("type") == "command" and command_is_guard(hook.get("command")):
                found = True
    problems = []
    if not found:
        problems.append(f"{name}: {SETTINGS} has no PreToolUse hook for the Bash tool that runs {HOOK_SCRIPT} by an anchored path "
                        "($HOME/workspace/hams_open/hams_shared/tools/claude_hooks/guard_signals.py); copy the entry from "
                        "hams_open/.claude/settings.json")
    if not is_tracked(repo_dir):
        problems.append(f"{name}: {SETTINGS} is not tracked by git (git add it): a fresh checkout or a worktree would run without the guard")
    return problems


# [@ANCHOR: check_claude_hook_registration:main]
def main(argv=None, out=None):
    """Check each directory in `argv` (default: the checkout this script is in); print the problems; return the exit code."""
    argv = sys.argv[1:] if argv is None else argv
    out = out or sys.stdout
    repos = argv or [os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
    for repo in repos:
        check_target.require_directory(repo, "check_claude_hook_registration")
    problems = [problem for repo in repos for problem in check_repo(repo)]
    for problem in problems:
        print(f"🚨 {problem}", file=out)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
