# SPDX-License-Identifier: AGPL-3.0-or-later
"""provision_build_host.sh do_apt must record for undo only the packages it installed."""
import os
import stat
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "provision_build_host.sh")

STUBS = {
    # sudo: `true` succeeds; `env ... apt-get` runs the stub apt-get; everything else (install, rm) is a no-op.
    "sudo": '#!/bin/sh\nshift\ncase "$1" in true) exit 0;; env) exec "$@";; esac\nexit 0\n',
    "apt-get": '#!/bin/sh\n[ "$1" = install ] && touch "$HOME/apt-ran"\nexit 0\n',
    "systemctl": "#!/bin/sh\nexit 0\n",
    "dpkg-query": '#!/bin/sh\nfor last; do :; done\n[ "$last" = pdns-server ] && { [ -n "$PDNS_PRESENT" ] || [ -e "$HOME/apt-ran" ]; } && { printf "install ok installed"; exit 0; }\nexit 1\n',
}


def _recorded(pdns_present):
    with tempfile.TemporaryDirectory() as tmp:
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir)
        for name, body in STUBS.items():
            path = os.path.join(bindir, name)
            with open(path, "w") as handle:
                handle.write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        env = dict(os.environ, HOME=tmp, PATH=bindir + ":" + os.environ["PATH"])
        if pdns_present:
            env["PDNS_PRESENT"] = "1"
        done = subprocess.run(["bash", "-c", f'source "{SCRIPT}"; do_apt'], capture_output=True, text=True, env=env, timeout=60, cwd=tmp)
        state = os.path.join(tmp, ".cache", "hams-lint", "provision", "apt-installed.txt")
        with open(state, encoding="utf-8") as handle:
            return done.returncode, handle.read().split()


class PdnsRecordTests(unittest.TestCase):
    def test_a_preinstalled_pdns_server_is_not_recorded_for_undo(self):
        code, recorded = _recorded(pdns_present=True)
        self.assertEqual(code, 0)
        self.assertIn("pdns-backend-sqlite3", recorded)
        self.assertNotIn("pdns-server", recorded)

    def test_a_pdns_server_pulled_in_by_the_backend_is_recorded(self):
        code, recorded = _recorded(pdns_present=False)
        self.assertEqual(code, 0)
        self.assertIn("pdns-server", recorded)


if __name__ == "__main__":
    unittest.main()
