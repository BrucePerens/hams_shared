# SPDX-License-Identifier: AGPL-3.0-or-later
"""Running programs for the restoration system: one place that builds environments and writes the log.

A secret never appears in an argument list, in the log, or on disk: it reaches a program only through the program's own
environment (`secret_env`). The log records the argument list and the NAMES of the secret variables, never their values.
"""
import os
import subprocess
import sys


# [@ANCHOR: runner:runner_restoration_error_class]
class RestorationError(Exception):
    """Any refusal or failure of the restoration system. The message says what to do next."""


SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", "HOME")


# [@ANCHOR: runner:runner_runner_class]
class Runner:
    """Runs commands with a minimal environment; `log` receives one line per command."""

    def __init__(self, log=None, base_env=None):
        self.log = log or (lambda line: sys.stderr.write(line + "\n"))
        source = os.environ if base_env is None else base_env
        self.base_env = {k: v for k, v in source.items() if k in SAFE_ENV_KEYS}
        self.base_env.setdefault("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")

    def run(self, argv, secret_env=None, extra_env=None, check=True, capture=False, stdin=None, cwd=None, timeout=None):
        """Run argv. Returns the CompletedProcess. RestorationError when check and the exit status is not 0.

        With capture, stdout is returned as text; stderr is always kept and quoted (a tail of it) in the error."""
        env = dict(self.base_env)
        env.update(extra_env or {})
        env.update(secret_env or {})
        names = sorted(secret_env or {})
        self.log("run: " + " ".join(argv) + (" [secret env: %s]" % ",".join(names) if names else ""))
        proc = subprocess.run(
            argv, env=env, check=False, cwd=cwd, timeout=timeout, stdin=stdin,
            stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        )
        if check and proc.returncode != 0:
            tail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip().splitlines()[-8:]
            raise RestorationError("%s exited %d: %s" % (argv[0], proc.returncode, " | ".join(tail)))
        return proc
