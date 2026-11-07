#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
A cached, isolated virtualenv for a daemon whose tests need packages that must never be installed system-wide
(hams_simulated_bots: aiortc, av, faster-whisper, piper-tts).

Why: a clean worktree, and every remote lint host, has no `daemons/<name>/.venv` (it is untracked), so the
daemon's pytest step failed to collect on `import av`. Installing those packages into the system python is wrong
(mypy would then type-check `import av` against PyAV's annotations). So the step runs through this file:

    python3 lint_daemon_venv.py --name NAME --requirements FILE -- -m pytest -q ...

which makes (or reuses) `<cache>/hams-lint-venvs/<NAME>-py<major><minor>-<16 hex of sha256(requirements)>`, outside
every worktree and shared by all of them, then replaces itself with that venv's python running the arguments after
`--`. The venv is built from the requirements file's content: a changed file is a new directory. The cache root is
`XDG_CACHE_HOME` or `~/.cache`, the same as lint_cache.py. Documentation: docs/runbooks/LINT_DAEMON_VENVS.md.
"""

import argparse
import contextlib
import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time

READY_MARKER = ".hams-lint-venv-ready"
KEEP_VENVS = 2  # newest venvs kept per name by prune_venvs
PRUNE_MIN_AGE_SECONDS = 3600  # a venv used within this long is never pruned (another run may be using it)
_VENV_TAG = re.compile(r"^(?P<name>.+)-py\d+-[0-9a-f]{16}$")


def _cache_root(environ):
    return environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")


# [@ANCHOR: lint_daemon_venv:venv_dir]
def venv_dir(name, requirements_path, environ=None, version_info=None):
    """The directory of the venv for `name` built from the content of `requirements_path`."""
    environ = os.environ if environ is None else environ
    version_info = sys.version_info if version_info is None else version_info
    with open(requirements_path, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()[:16]
    tag = f"{name}-py{version_info[0]}{version_info[1]}-{digest}"
    return os.path.join(_cache_root(environ), "hams-lint-venvs", tag)


# [@ANCHOR: lint_daemon_venv:ensure_venv]
def ensure_venv(name, requirements_path, environ=None, run=subprocess.run):
    """The path of the venv's python, building the venv first when it does not exist yet.

    Exclusive flock on `<dir>.lock` for the whole build, so two lint steps (two worktrees, two runs) never build
    the same venv at once; the second finds it ready. The build goes into `<dir>.partial` and is renamed into
    place only after `pip install -r` succeeded, so a killed build leaves nothing that looks complete. Raises
    RuntimeError, with the pip output, when the build fails."""
    target = venv_dir(name, requirements_path, environ)
    python = os.path.join(target, "bin", "python3")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target + ".lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker_path = os.path.join(target, READY_MARKER)
        if os.path.exists(marker_path):
            os.utime(marker_path)  # the marker's mtime is the venv's last use (prune_venvs keeps the newest)
            return python
        partial = target + ".partial"
        shutil.rmtree(partial, ignore_errors=True)
        shutil.rmtree(target, ignore_errors=True)
        for argv in (
            [sys.executable, "-m", "venv", partial],
            [os.path.join(partial, "bin", "python3"), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
             "-r", requirements_path],
        ):
            result = run(argv, capture_output=True, text=True)
            if result.returncode != 0:
                shutil.rmtree(partial, ignore_errors=True)
                raise RuntimeError(f"building the {name} lint venv failed ({' '.join(argv[:4])}):\n{result.stdout}{result.stderr}")
        with open(os.path.join(partial, READY_MARKER), "w", encoding="utf-8") as marker:
            marker.write(requirements_path + "\n")
        os.rename(partial, target)
    prune_venvs(name, environ=environ)
    return python


# [@ANCHOR: lint_daemon_venv:prune_venvs]
def prune_venvs(name, keep=KEEP_VENVS, environ=None, now=None, min_age=PRUNE_MIN_AGE_SECONDS):
    """Deletes all but the `keep` most recently used cached venvs of `name`; returns the directories removed.

    Only complete venvs of exactly this `name` count (`fine_tuning`'s venv is not `hams_simulated_bots`'s). Recency is
    the mtime of the ready marker, which `ensure_venv` touches on every use. A venv used less than `min_age` seconds ago
    is never removed, and neither is one whose `<dir>.lock` is held (being built); a leftover `<dir>.partial` older than
    `min_age` of this name is removed too. Never raises for a directory it cannot remove."""
    environ = os.environ if environ is None else environ
    now = time.time() if now is None else now
    base = os.path.join(_cache_root(environ), "hams-lint-venvs")
    found = []
    removed = []
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return removed
    for entry in entries:
        path = os.path.join(base, entry)
        match = _VENV_TAG.match(entry[: -len(".partial")] if entry.endswith(".partial") else entry)
        if not match or match.group("name") != name or not os.path.isdir(path):
            continue
        if entry.endswith(".partial"):
            if now - os.path.getmtime(path) > min_age:
                shutil.rmtree(path, ignore_errors=True)
                removed.append(path)
            continue
        marker = os.path.join(path, READY_MARKER)
        if os.path.exists(marker):
            found.append((os.path.getmtime(marker), path))
    found.sort(reverse=True)
    for used, path in found[keep:]:
        if now - used < min_age:
            continue
        with open(path + ".lock", "a", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path)
        with contextlib.suppress(OSError):
            os.remove(path + ".lock")
    return removed


# [@ANCHOR: lint_daemon_venv:main]
def main(argv=None, environ=None, execv=os.execv):
    """Command line: ensure the venv, then exec its python with the arguments after `--`. Returns 2 for a failed
    build (the message is on stderr); a successful exec never returns."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--name", required=True)
    parser.add_argument("--requirements", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="after `--`: arguments for the venv's python")
    options = parser.parse_args(argv)
    command = options.command[1:] if options.command[:1] == ["--"] else options.command
    try:
        python = ensure_venv(options.name, options.requirements, environ)
    except (RuntimeError, OSError) as error:
        print(error, file=sys.stderr)
        return 2
    execv(python, [python] + command)
    return 0


if __name__ == "__main__":
    sys.exit(main())
