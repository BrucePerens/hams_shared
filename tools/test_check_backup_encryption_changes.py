#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests for check_backup_encryption_changes.py: a change to backup encryption, stanza creation, key generation or a secret-file overwrite in
provisioning and backup code fails the lint unless the same change adds an entry naming Bruce's approval date to
docs/APPROVED_BACKUP_ENCRYPTION_CHANGES.md (Bruce, 2026-11-09). Real git repositories in a temporary directory.

Tests [@ANCHOR: check_backup_encryption_changes:parse_diff]
Tests [@ANCHOR: check_backup_encryption_changes:relevant_path]
Tests [@ANCHOR: check_backup_encryption_changes:findings_for_hunks]
Tests [@ANCHOR: check_backup_encryption_changes:parse_approvals]
Tests [@ANCHOR: check_backup_encryption_changes:is_approved]
Tests [@ANCHOR: check_backup_encryption_changes:check_repo]
Tests [@ANCHOR: check_backup_encryption_changes:main]
"""

import datetime
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check_backup_encryption_changes as chk  # noqa: E402

DIFF = """diff --git a/tools/b2_backup.py b/tools/b2_backup.py
--- a/tools/b2_backup.py
+++ b/tools/b2_backup.py
@@ -1,0 +2,2 @@
+x = 1
+y = 2
@@ -9 +10 @@
-old = 1
+new = 1
diff --git a/gone.py b/gone.py
--- a/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-bye
"""
APPROVAL = "- tools/provision.py | keygen | approved by Bruce on 2026-11-09 | rebuild of the test box"


class PureTests(unittest.TestCase):
    def test_parse_diff(self):
        files = chk.parse_diff(DIFF)
        self.assertEqual(files["tools/b2_backup.py"], [(["x = 1", "y = 2"], []), (["new = 1"], ["old = 1"])])
        self.assertNotIn("gone.py", files)
        self.assertEqual(chk.parse_diff(""), {})

    def test_parse_diff_keeps_a_removed_line_that_starts_with_two_dashes_and_a_space(self):
        text = "diff --git a/t.sql b/t.sql\n--- a/t.sql\n+++ b/t.sql\n@@ -3,2 +3 @@\n--- repo1-cipher-pass note\n-keep\n+new\n"
        self.assertEqual(chk.parse_diff(text), {"t.sql": [(["new"], ["-- repo1-cipher-pass note", "keep"])]})

    def test_parse_diff_keeps_an_added_line_that_starts_with_two_pluses_and_a_space(self):
        text = "diff --git a/t b/t\n--- a/t\n+++ b/t\n@@ -0,0 +1,2 @@\n+++ b/provision_y.py\n+tail\n"
        self.assertEqual(chk.parse_diff(text), {"t": [(["++ b/provision_y.py", "tail"], [])]})

    def test_parse_diff_drops_the_tab_git_adds_after_a_path_with_a_space(self):
        text = "diff --git a/b c.py b/b c.py\n--- a/b c.py\t\n+++ b/b c.py\t\n@@ -1,0 +2 @@\n+x\n"
        self.assertEqual(list(chk.parse_diff(text)), ["b c.py"])

    def test_relevant_path(self):
        for path in ("tools/provision.py", "tools/infrastructure.py", "tools/b2_backup.py", "backup_management/models/backup_config.py", "daemon_key_manager/models/key_registry.py",
                     "devbox_tools/hams1-base-backup.service", "devbox_tools/hams1_storage_backup.py", "tools/pgbackrest_setup.sh", "devbox_tools/mirror_repos_to_b2.py"):
            self.assertTrue(chk.relevant_path(path), path)
        for path in ("tools/test_b2_backup.py", "backup_management/tests/test_backup.py", "tools/claude_hooks/guard_signals.py", "tools/README.md",
                     "docs/runbooks/b2_backup.md", "backup_management/static/src/x.js", "tools/run_linters.py", "zero_sudo/models/security_utils.py",
                     "backup_management/views/backup_config_views.xml", "tools/check_backup_encryption_changes.py"):
            self.assertFalse(chk.relevant_path(path), path)

    def rules(self, added, removed=()):
        return sorted({rule for _, rule, _ in chk.findings_for_hunks("tools/x.py", [(list(added), list(removed))])})

    def test_findings_for_hunks(self):
        self.assertEqual(self.rules(["repo3-cipher-type=aes-256-cbc"]), ["cipher"])
        self.assertEqual(self.rules([], ["repo2-cipher-pass=x"]), ["cipher"], "removing a cipher setting is a change too")
        self.assertEqual(self.rules(["run(['pgbackrest', 'stanza-create'])"]), ["stanza"])
        self.assertEqual(self.rules(['cmd = ["kopia", "repository", "change-password"]']), ["kopia-repo"])
        self.assertEqual(self.rules(['env_vars["HAMS_CRYPTO_KEY"] = base64.b64encode(secrets.token_bytes(32))']), ["keygen"])
        self.assertEqual(self.rules(["key = os.urandom(32)", "open('/var/lib/odoo/hams_crypto.secret', 'w').write(key)"]), ["keygen", "secret-write"])
        self.assertEqual(self.rules(["shutil.copy(src, '/etc/pgbackrest/pgbackrest.conf')"]), ["secret-write"])
        self.assertEqual(self.rules(["os.replace(tmp, '/opt/hams/etc/b2_backup/b2_backup.env')"]), ["secret-write"])
        self.assertEqual(self.rules(["token = secrets.token_urlsafe(48)", "f.write(token)"]), [], "a generated secret that is no backup key")
        self.assertEqual(self.rules(["with open('/var/lib/odoo/hams_crypto.secret') as f:", "    key = f.read()"]), [])
        self.assertEqual(self.rules(["x = 1"]), [])
        self.assertEqual(self.rules([], ["x = secrets.token_bytes(8)  # HAMS_CRYPTO_KEY"]), [], "removed keygen is not scanned")

    def test_parse_approvals(self):
        today = datetime.date(2026, 11, 9)
        ok = chk.parse_approvals([APPROVAL, "- a/*.py | * | approved by Bruce on 2026-11-01 | x", "- b | cipher,stanza | approved by Bruce on 2026-10-04"], today)
        self.assertEqual([(a[0], sorted(a[1])) for a in ok], [("tools/provision.py", ["keygen"]), ("a/*.py", ["*"]), ("b", ["cipher", "stanza"])])
        for bad in ("- a | keygen | approved by Claude on 2026-11-01 | x", "- a | keygen | approved by Bruce on 2026-11-10 | future", "- a | keygen | approved by Bruce on 2026-13-01",
                    "- a | nonsense | approved by Bruce on 2026-11-01", "- a | keygen | approved by Bruce | no date", "a | keygen | approved by Bruce on 2026-11-01", "prose"):
            self.assertEqual(chk.parse_approvals([bad], today), [], bad)

    def test_is_approved(self):
        approvals = [("tools/provision.py", {"keygen"}, None), ("backup_management/*", {"*"}, None), ("b2_backup.py", {"cipher"}, None)]
        self.assertTrue(chk.is_approved("tools/provision.py", "keygen", approvals))
        self.assertFalse(chk.is_approved("tools/provision.py", "cipher", approvals))
        self.assertTrue(chk.is_approved("backup_management/models/x.py", "stanza", approvals))
        self.assertTrue(chk.is_approved("tools/b2_backup.py", "cipher", approvals))
        self.assertFalse(chk.is_approved("tools/other.py", "keygen", approvals))


class RepoTests(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="backup-enc-")
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.org")
        self.git("config", "user.name", "t")
        self.write("tools/provision.py", "print('x')\n")
        self.write("docs/APPROVED_BACKUP_ENCRYPTION_CHANGES.md", "# approvals\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("checkout", "-q", "-b", "work")

    def git(self, *args):
        subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True)

    def write(self, name, text, mode="w"):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode) as handle:
            handle.write(text)

    def test_a_clean_change_passes(self):
        self.write("tools/provision.py", "print('y')\n")
        self.assertEqual(chk.check_repo(self.repo), [])

    def test_a_committed_cipher_change_fails_and_names_the_file(self):
        self.write("tools/provision.py", "conf = 'repo3-cipher-type=none'\n")
        self.git("commit", "-qam", "cipher")
        problems = chk.check_repo(self.repo)
        self.assertEqual(len(problems), 1)
        self.assertIn("tools/provision.py", problems[0])
        self.assertIn("cipher", problems[0])

    def test_an_uncommitted_and_an_untracked_change_fail_too(self):
        self.write("tools/provision.py", "k = secrets.token_bytes(32)  # HAMS_CRYPTO_KEY\n")
        self.assertTrue(chk.check_repo(self.repo))
        self.git("checkout", "-q", "--", "tools/provision.py")
        self.write("tools/new_backup_tool.py", "run(['pgbackrest', 'stanza-create'])\n")
        self.assertTrue(chk.check_repo(self.repo))

    def test_an_entry_added_in_the_same_change_approves_it(self):
        self.write("tools/provision.py", "k = secrets.token_bytes(32)  # HAMS_CRYPTO_KEY\n")
        self.write("docs/APPROVED_BACKUP_ENCRYPTION_CHANGES.md", "# approvals\n" + APPROVAL + "\n")
        self.assertEqual(chk.check_repo(self.repo, today=datetime.date(2026, 11, 9)), [])

    def test_an_old_entry_licenses_nothing_new(self):
        self.write("docs/APPROVED_BACKUP_ENCRYPTION_CHANGES.md", "# approvals\n" + APPROVAL + "\n")
        self.git("commit", "-qam", "approval alone")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.write("tools/provision.py", "k = secrets.token_bytes(32)  # HAMS_CRYPTO_KEY\n")
        self.assertTrue(chk.check_repo(self.repo, today=datetime.date(2026, 11, 9)))

    def test_an_entry_for_another_rule_or_file_does_not_approve(self):
        self.write("tools/provision.py", "conf = 'repo3-cipher-type=none'\n")
        self.write("docs/APPROVED_BACKUP_ENCRYPTION_CHANGES.md", "# approvals\n" + APPROVAL + "\n")
        self.assertTrue(chk.check_repo(self.repo, today=datetime.date(2026, 11, 9)))

    def test_tests_docs_and_unrelated_files_are_not_scanned(self):
        self.write("tools/test_provision.py", "x = 'repo3-cipher-type=none'\n")
        self.write("docs/runbook.md", "stanza-create\n")
        self.write("tools/other.py", "x = 'stanza-create'\n")
        self.assertEqual(chk.check_repo(self.repo), [])

    def test_the_root_marker_works_for_a_repository_whose_docs_is_a_link(self):
        self.write("tools/provision.py", "k = secrets.token_bytes(32)  # HAMS_CRYPTO_KEY\n")
        self.write("APPROVED_BACKUP_ENCRYPTION_CHANGES.md", APPROVAL + "\n")
        self.assertEqual(chk.check_repo(self.repo, today=datetime.date(2026, 11, 9)), [])

    def test_main_prints_and_returns_one_or_zero_and_passes_without_a_base(self):
        out = io.StringIO()
        self.assertEqual(chk.main([self.repo], out), 0)
        self.write("tools/provision.py", "conf = 'repo3-cipher-type=none'\n")
        out = io.StringIO()
        self.assertEqual(chk.main([self.repo], out), 1)
        self.assertIn("🚨", out.getvalue())
        self.assertEqual(chk.main([self.repo, "--base", "origin/main"], io.StringIO()), 1)
        self.assertEqual(chk.main([self.repo, "--base", "no/such/ref"], io.StringIO()), 0)
        self.assertEqual(chk.main([tempfile.gettempdir()], io.StringIO()), 0)

    def test_the_script_runs_from_the_command_line(self):
        self.write("tools/provision.py", "conf = 'repo3-cipher-type=none'\n")
        done = subprocess.run([sys.executable, os.path.join(HERE, "check_backup_encryption_changes.py"), self.repo], capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)
        self.assertIn("cipher", done.stdout)


if __name__ == "__main__":
    unittest.main()
