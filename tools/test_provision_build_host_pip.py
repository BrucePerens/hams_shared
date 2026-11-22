# SPDX-License-Identifier: AGPL-3.0-or-later
"""provision_build_host.sh must not report "nothing missing" when it cannot read the python pins."""
import os
import stat
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "provision_build_host.sh")


def _run(mode, stubs, tmp):
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir, exist_ok=True)
    for name, body in stubs.items():
        path = os.path.join(bindir, name)
        with open(path, "w") as handle:
            handle.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    env = dict(os.environ, HOME=tmp, PATH=bindir + ":" + os.environ["PATH"], HAMS_REQUIREMENTS=os.path.join(tmp, "no-such-requirements.txt"))
    done = subprocess.run(["bash", SCRIPT, mode], capture_output=True, text=True, env=env, timeout=120, cwd=tmp)
    return done.returncode, done.stdout + done.stderr


class UnreadablePinsTests(unittest.TestCase):
    def test_plan_says_the_pins_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            _code, out = _run("plan", {}, tmp)
        self.assertIn("cannot read the pins", out)
        self.assertNotIn("hash-pinned): none", out)

    def test_apply_user_fails_instead_of_reporting_nothing_missing(self):
        stubs = {
            "rustup": '#!/bin/sh\ncase "$1" in toolchain) echo "1.98.1-x86_64-unknown-linux-gnu";; target) echo wasm32-unknown-unknown;; esac\nexit 0\n',
            "psql": "#!/bin/sh\nexit 1\n",
            "pg_lsclusters": "#!/bin/sh\nexit 0\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            code, out = _run("apply-user", stubs, tmp)
        self.assertNotIn("python: nothing missing", out)
        self.assertIn("cannot read the pins", out)
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
