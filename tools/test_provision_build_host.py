# SPDX-License-Identifier: AGPL-3.0-or-later
"""provision_build_host.sh verify must report a failing cargo, node, pytest or kopia as FAIL, not hide it behind a pipe."""
import os
import stat
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "provision_build_host.sh")

STUBS = {
    # cargo new makes the crate; test fails (as a broken toolchain would); clippy and fmt pass.
    "cargo": '#!/bin/sh\nif [ "$1" = new ]; then mkdir -p "$5/src" 2>/dev/null || mkdir -p smoke/src; touch smoke/src/lib.rs; exit 0; fi\n'
             '[ "$1" = test ] && { echo "test result: FAILED"; exit 101; }\nexit 0\n',
    "node": '#!/bin/sh\necho "# pass 0"\necho "# fail 1"\nexit 1\n',
    "kopia": "#!/bin/sh\nexit 1\n",
    "psql": "#!/bin/sh\nexit 1\n",
    "createdb": "#!/bin/sh\nexit 1\n",
    "dropdb": "#!/bin/sh\nexit 0\n",
    "google-chrome": "#!/bin/sh\nexit 1\n",
    "python3": '#!/bin/sh\ncase "$*" in *pytest*) echo "1 failed"; exit 1;; esac\nexec /usr/bin/python3 "$@"\n',
}


class VerifyReportsFailures(unittest.TestCase):
    def test_failing_tools_behind_pipes_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            bindir = os.path.join(tmp, "bin")
            os.makedirs(bindir)
            for name, body in STUBS.items():
                path = os.path.join(bindir, name)
                with open(path, "w") as handle:
                    handle.write(body)
                os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
            env = dict(os.environ, HOME=tmp, PATH=bindir + ":" + os.environ["PATH"])
            done = subprocess.run(["bash", SCRIPT, "verify"], capture_output=True, text=True, env=env, timeout=120, cwd=tmp)
        out = done.stdout + done.stderr
        for what in ("FAIL cargo", "FAIL node", "FAIL pytest", "FAIL kopia"):
            self.assertIn(what, out)
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("VERIFY OK", out)


if __name__ == "__main__":
    unittest.main()
