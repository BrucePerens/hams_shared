#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for infrastructure.py's smaller, more self-contained pieces:
OS-identification, path/permission helpers, the download/keyring/pycache
provisioning hooks (each already designed for testability via an injected
run_cmd_func rather than shelling out directly), and password generation.

infrastructure.py is 2000+ lines of real system provisioning (systemd
units, PostgreSQL, Odoo database bootstrap, static-file layout) -- the
large environment-level orchestration functions (provision_environment,
initialize_odoo_database, run_post_provision_smoketest, and friends) are
genuinely destructive/host-dependent and not attempted here, matching
test_provision.py's own reasoning for why provision() itself is tested by
mocking infrastructure wholesale rather than executing it for real. This
file covers the smaller units that logic actually lives in.
"""

import builtins
import hashlib
import inspect
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import infrastructure as infra

_REAL_OPEN = builtins.open


class _SafePatchTestCase(unittest.TestCase):
    """Matches test_provision.py's own convention: a self.safe_patch()
    wrapper instead of a bare `with patch(...)` context manager or
    `@patch` decorator at each call site."""

    def safe_patch(self, target, *args, **kwargs):
        patcher = patch(target, *args, **kwargs)
        mock_obj = patcher.start()
        self.addCleanup(patcher.stop)
        return mock_obj

    def safe_patch_object(self, target, attribute, *args, **kwargs):
        patcher = patch.object(target, attribute, *args, **kwargs)
        mock_obj = patcher.start()
        self.addCleanup(patcher.stop)
        return mock_obj


class _TmpDirTestCase(_SafePatchTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        # _hook_failures is module-level state shared by every test in this
        # process -- reset it before and after each test so one test's
        # recorded failures can never leak into another's assertions.
        infra.reset_hook_failures()
        self.addCleanup(infra.reset_hook_failures)


def _write_os_release(path, id_line=None, codename_line=None):
    with open(path, "w") as f:
        if id_line is not None:
            f.write(f'ID="{id_line}"\n')
        if codename_line is not None:
            f.write(f'VERSION_CODENAME={codename_line}\n')


class GetPgBinTests(_SafePatchTestCase):
    def test_picks_the_highest_numeric_major_version_not_the_lexicographically_last_path(self):
        # A bare `sorted(paths)[-1]` ranks "9.6" above "14" and "12" (string
        # '9' > '1'), so a box with both an old 9.6 cluster and a newer 14
        # installed side by side (e.g. mid-upgrade) would silently get the
        # ancient binary. Real version-number ordering must win instead.
        fake_paths = [
            "/usr/lib/postgresql/9.6/bin/psql",
            "/usr/lib/postgresql/12/bin/psql",
            "/usr/lib/postgresql/14/bin/psql",
        ]
        self.safe_patch_object(infra.glob, "glob", return_value=fake_paths)
        self.assertEqual(infra.get_pg_bin("psql"), "/usr/lib/postgresql/14/bin/psql")

    def test_two_digit_and_one_digit_majors_still_sort_numerically(self):
        fake_paths = [
            "/usr/lib/postgresql/9/bin/psql",
            "/usr/lib/postgresql/10/bin/psql",
        ]
        self.safe_patch_object(infra.glob, "glob", return_value=fake_paths)
        self.assertEqual(infra.get_pg_bin("psql"), "/usr/lib/postgresql/10/bin/psql")

    def test_falls_back_to_shutil_which_when_no_versioned_dir_exists(self):
        self.safe_patch_object(infra.glob, "glob", return_value=[])
        self.safe_patch_object(infra.shutil, "which", return_value="/usr/bin/psql")
        self.assertEqual(infra.get_pg_bin("psql"), "/usr/bin/psql")


class GetOsIdentifierTests(_TmpDirTestCase):
    def test_reads_the_real_id_field_from_os_release(self):
        path = os.path.join(self.tmp, "os-release")
        _write_os_release(path, id_line="debian")
        self.safe_patch("builtins.open", side_effect=lambda p, *a, **kw: _REAL_OPEN(path) if p == "/etc/os-release" else _REAL_OPEN(p, *a, **kw))
        self.assertEqual(infra.get_os_identifier(), "debian")

    def test_falls_back_to_ubuntu_when_os_release_is_unreadable(self):
        self.safe_patch("builtins.open", side_effect=OSError("no such file"))
        self.assertEqual(infra.get_os_identifier(), "ubuntu")


class GetOsCodenameTests(_TmpDirTestCase):
    def test_reads_the_real_codename_field_from_os_release(self):
        path = os.path.join(self.tmp, "os-release")
        _write_os_release(path, codename_line="bookworm")
        self.safe_patch("builtins.open", side_effect=lambda p, *a, **kw: _REAL_OPEN(path) if p == "/etc/os-release" else _REAL_OPEN(p, *a, **kw))
        self.assertEqual(infra.get_os_codename(), "bookworm")

    def test_falls_back_to_jammy_when_os_release_is_unreadable(self):
        self.safe_patch("builtins.open", side_effect=OSError("no such file"))
        self.assertEqual(infra.get_os_codename(), "jammy")


class FormatEnvTests(unittest.TestCase):
    def test_empty_text_returns_empty_string(self):
        self.assertEqual(infra.format_env("", {"X": "1"}), "")
        self.assertEqual(infra.format_env(None, {"X": "1"}), "")

    def test_substitutes_a_real_variable(self):
        self.assertEqual(infra.format_env("host={DOMAIN}", {"DOMAIN": "hams.com"}), "host=hams.com")

    def test_the_key_bootstrapper_unit_names_the_real_database(self):
        # The unit template once said DB=${DB_NAME}; format_env() turned that into "$hams_prod",
        # a shell variable that does not exist, so the service silently fell back to the
        # hams_test database and failed on the first real production release (2026-09-21).
        entry = next(
            item for item in infra.MANIFEST["static_files"]
            if item.get("path") == "/opt/hams/systemd/hams.daemon.keys.service"
        )
        text = infra.format_env(entry["content"], {"DB_NAME": "hams_prod"})
        self.assertIn('DB=hams_prod;', text)
        self.assertNotIn("$hams_prod", text)

    def test_the_key_bootstrapper_commits_what_succeeded_then_fails_on_a_partial_result(self):
        # action_force_provision_all() now returns a "danger" result instead of raising when a daemon fails, so
        # the unit must commit what succeeded (files are already written) and then fail the unit itself.
        entry = next(
            item for item in infra.MANIFEST["static_files"]
            if item.get("path") == "/opt/hams/systemd/hams.daemon.keys.service"
        )
        text = infra.format_env(entry["content"], {"DB_NAME": "hams_prod"})
        command = next(line for line in text.splitlines() if line.startswith("ExecStart="))
        self.assertLess(command.index("env.cr.commit()"), command.index("assert r['params']['type'] == 'success'"))
        self.assertIn("r = env['daemon.key.registry'].action_force_provision_all()", command)

    def test_a_missing_variable_now_raises_instead_of_silently_degrading(self):
        # Real fix, 2026-09-12 (hams_shared/tools/ 326-finding discovery, CRITICAL AI
        # LAZINESS: Catch-all KeyError): this used to silently return the unformatted
        # template instead of raising, on the theory that "a hook with an unresolved
        # placeholder shouldn't crash the whole provisioning run" -- but format_env() is
        # never actually called on a hook's own template text (see format_env's own
        # docstring for the full, checked-against-every-real-call-site rationale). Every
        # real caller is provision_static_files() writing a path/src/url/content to disk;
        # a missing key there always means a real authoring bug, and used to mean silently
        # writing a real file to disk with a literal, unresolved "{VAR}" while reporting
        # success. See test_provision_static_files_raises_loudly_on_an_unresolved_placeholder
        # in ProvisionStaticFilesPermissionTests below for that end-to-end case.
        with self.assertRaises(KeyError):
            infra.format_env("host={MISSING}", {})


class SafeRemoveTests(_TmpDirTestCase):
    def test_removes_a_real_existing_file(self):
        path = os.path.join(self.tmp, "f.txt")
        with open(path, "w") as f:
            f.write("x")
        infra.safe_remove(path)
        self.assertFalse(os.path.exists(path))

    def test_a_missing_file_is_a_silent_no_op(self):
        infra.safe_remove(os.path.join(self.tmp, "nope.txt"))  # must not raise


class ApplyPermissionsTests(_TmpDirTestCase):
    def test_applies_mode_only_when_no_owner_given(self):
        path = os.path.join(self.tmp, "f.txt")
        with open(path, "w") as f:
            f.write("x")
        infra.apply_permissions(path, None, 0o600)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_applies_chown_when_a_real_owner_string_resolves(self):
        path = os.path.join(self.tmp, "f.txt")
        with open(path, "w") as f:
            f.write("x")
        mock_pwd = self.safe_patch_object(infra.pwd, "getpwnam")
        mock_pwd.return_value.pw_uid = 4242
        mock_grp = self.safe_patch_object(infra.grp, "getgrnam")
        mock_grp.return_value.gr_gid = 4343
        mock_chown = self.safe_patch_object(infra.os, "chown")
        infra.apply_permissions(path, "someuser:somegroup", 0o644)
        mock_chown.assert_called_once_with(path, 4242, 4343)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)

    def test_an_unresolvable_owner_string_skips_chown_but_still_chmods(self):
        path = os.path.join(self.tmp, "f.txt")
        with open(path, "w") as f:
            f.write("x")
        self.safe_patch_object(infra.pwd, "getpwnam", side_effect=KeyError("no such user"))
        mock_chown = self.safe_patch_object(infra.os, "chown")
        infra.apply_permissions(path, "nosuchuser:nosuchgroup", 0o644)
        mock_chown.assert_not_called()
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)

    def test_a_chmod_failure_is_swallowed_not_raised(self):
        mock_chmod = self.safe_patch_object(infra.os, "chmod", side_effect=OSError("simulated"))
        infra.apply_permissions(os.path.join(self.tmp, "f.txt"), None, 0o644)  # must not raise
        mock_chmod.assert_called_once()


class DownloadFileTests(_TmpDirTestCase):
    def test_writes_the_real_response_body_to_the_destination_path(self):
        mock_response = MagicMock()
        mock_response.read.return_value = b"file contents"
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = False
        self.safe_patch_object(infra.urllib.request, "urlopen", return_value=mock_response)

        dest = os.path.join(self.tmp, "downloaded.bin")
        infra.download_file("https://example.invalid/file", dest, 0o644, {})

        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"file contents")

    def test_a_network_failure_writes_an_empty_file_rather_than_crashing(self):
        self.safe_patch_object(infra.urllib.request, "urlopen", side_effect=OSError("simulated network partition"))
        dest = os.path.join(self.tmp, "downloaded.bin")
        infra.download_file("https://example.invalid/file", dest, 0o644, {})
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"")

    def test_a_network_failure_keeps_a_copy_that_is_already_installed(self):
        dest = os.path.join(self.tmp, "downloaded.bin")
        with open(dest, "wb") as f:
            f.write(b"good existing key")
        self.safe_patch_object(infra.urllib.request, "urlopen", side_effect=OSError("simulated timeout"))
        infra.download_file("https://example.invalid/file", dest, 0o644, {})
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"good existing key")

    def test_the_configured_user_agent_env_var_is_sent(self):
        captured = {}

        def fake_urlopen(req, timeout=5):
            captured["ua"] = req.headers.get("User-agent")
            m = MagicMock()
            m.read.return_value = b""
            m.__enter__.return_value = m
            m.__exit__.return_value = False
            return m

        self.safe_patch_object(infra.urllib.request, "urlopen", side_effect=fake_urlopen)
        infra.download_file("https://example.invalid/file", os.path.join(self.tmp, "f"), 0o644, {"SYSTEM_USER_AGENT": "MyAgent/1.0"})
        self.assertEqual(captured["ua"], "MyAgent/1.0")


class HookGenerateSslTests(_TmpDirTestCase):
    def test_generates_certs_via_run_cmd_func_when_none_exist_yet(self):
        fullchain = os.path.join(self.tmp, "fullchain.pem")

        def fake_run_cmd(cmd):
            # Simulate openssl actually producing the cert files.
            with open(fullchain, "w") as f:
                f.write("cert")
            with open(os.path.join(self.tmp, "privkey.pem"), "w") as f:
                f.write("key")

        mock_run = MagicMock(side_effect=fake_run_cmd)
        infra.hook_generate_ssl({"DOMAIN": "hams.com"}, "", self.tmp, mock_run)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertIn("openssl", cmd)
        self.assertIn("CN=hams.com", cmd[-1])
        # The LoTW copy only happens once a real fullchain.pem exists.
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "lotw_root.pem")))

    def test_does_nothing_when_a_fullchain_already_exists(self):
        with open(os.path.join(self.tmp, "fullchain.pem"), "w") as f:
            f.write("existing cert")
        mock_run = MagicMock()
        infra.hook_generate_ssl({}, "", self.tmp, mock_run)
        mock_run.assert_not_called()

    def test_a_run_cmd_failure_is_swallowed_and_no_lotw_copy_happens(self):
        mock_run = MagicMock(side_effect=RuntimeError("simulated openssl failure"))
        infra.hook_generate_ssl({}, "", self.tmp, mock_run)  # must not raise
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "lotw_root.pem")))


class HookCreatePdnsSqliteSchemaTests(_TmpDirTestCase):
    def test_creates_schema_via_sqlite3_stdin_when_no_db_exists_yet(self):
        db_path = os.path.join(self.tmp, "pdns.sqlite3")
        schema_path = os.path.join(self.tmp, "schema.sqlite3.sql")
        with open(schema_path, "w") as f:
            f.write("CREATE TABLE domains (id INTEGER);")
        self.safe_patch(
            "infrastructure.os.path.exists",
            side_effect=lambda p: p in (schema_path,),
        )
        self.safe_patch("infrastructure._PDNS_SQLITE_SCHEMA_PATH", schema_path)
        self.safe_patch("infrastructure.apply_permissions")
        mock_run = MagicMock()
        infra.hook_create_pdns_sqlite_schema({}, "", self.tmp, mock_run)

        mock_run.assert_called_once()
        (cmd,), kwargs = mock_run.call_args
        self.assertEqual(cmd, ["sqlite3", db_path])
        self.assertIn("stdin", kwargs)
        infra.apply_permissions.assert_called_once_with(db_path, "pdns:pdns", 0o664)

    def test_does_nothing_when_the_database_already_exists(self):
        db_path = os.path.join(self.tmp, "pdns.sqlite3")
        with open(db_path, "w") as f:
            f.write("existing db")
        mock_run = MagicMock()
        infra.hook_create_pdns_sqlite_schema({}, "", self.tmp, mock_run)
        mock_run.assert_not_called()

    def test_missing_packaged_schema_file_is_a_recorded_failure_not_a_raise(self):
        # A path that genuinely doesn't exist, rather than trusting this
        # box's own real pdns-backend-sqlite3 install state (which does
        # have the real schema file, so patching only os.path.exists
        # would still resolve the real _PDNS_SQLITE_SCHEMA_PATH to True).
        self.safe_patch(
            "infrastructure._PDNS_SQLITE_SCHEMA_PATH",
            os.path.join(self.tmp, "does-not-exist.sql"),
        )
        mock_run = MagicMock()
        infra.hook_create_pdns_sqlite_schema({}, "", self.tmp, mock_run)  # must not raise
        mock_run.assert_not_called()
        names = [name for name, _ in infra.get_hook_failures()]
        self.assertIn("hook_create_pdns_sqlite_schema", names)


class HookClearPycacheTests(_TmpDirTestCase):
    def test_removes_every_entry_under_the_pycache_dir(self):
        pycache = os.path.join(self.tmp, "pycache")
        os.makedirs(os.path.join(pycache, "subdir"))
        with open(os.path.join(pycache, "a.pyc"), "w") as f:
            f.write("x")
        infra.hook_clear_pycache({}, self.tmp, pycache, MagicMock())
        self.assertEqual(os.listdir(pycache), [])

    def test_recompiles_daemons_when_a_daemons_dir_exists_under_dest_dir(self):
        pycache = os.path.join(self.tmp, "pycache")
        os.makedirs(pycache)
        daemons_dir = os.path.join(self.tmp, "opt", "hams", "daemons")
        os.makedirs(daemons_dir)
        mock_compile = self.safe_patch_object(infra.compileall, "compile_dir")
        infra.hook_clear_pycache({}, self.tmp, pycache, MagicMock())
        mock_compile.assert_called_once_with(daemons_dir, quiet=1)

    def test_a_missing_pycache_dir_is_a_silent_no_op_for_the_removal_step(self):
        infra.hook_clear_pycache({}, self.tmp, os.path.join(self.tmp, "nope"), MagicMock())  # must not raise


class HookInstallKeyringTests(_TmpDirTestCase):
    def test_hook_install_odoo_key_dearmors_into_the_odoo_keyring_path(self):
        key_path = os.path.join(self.tmp, "downloaded.key")
        with open(key_path, "w") as f:
            f.write("armored key data")
        mock_run = MagicMock()
        infra.hook_install_odoo_key({}, self.tmp, key_path, mock_run)
        out = os.path.join(self.tmp, "usr/share/keyrings/odoo-archive-keyring.gpg")
        mock_run.assert_called_once_with(["gpg", "--dearmor", "-o", out, "--yes", key_path])
        self.assertFalse(os.path.exists(key_path))

    def test_hook_install_pg_key_dearmors_into_the_postgresql_keyring_path(self):
        key_path = os.path.join(self.tmp, "downloaded.key")
        with open(key_path, "w") as f:
            f.write("armored key data")
        mock_run = MagicMock()
        infra.hook_install_pg_key({}, self.tmp, key_path, mock_run)
        out = os.path.join(self.tmp, "usr/share/keyrings/postgresql-keyring.gpg")
        mock_run.assert_called_once_with(["gpg", "--dearmor", "-o", out, "--yes", key_path])
        self.assertFalse(os.path.exists(key_path))


class HookInstallKopiaBinaryTests(_TmpDirTestCase):
    # KOPIA_ARCH is always passed explicitly in env_vars here (rather than left for
    # _kopia_release_arch to auto-resolve via a real dpkg-architecture call) so these
    # tests are deterministic regardless of the real architecture of whatever machine
    # actually runs them -- _kopia_release_arch itself has its own dedicated tests below.
    def test_extracts_and_chmods_the_kopia_binary_via_run_cmd_func(self):
        archive_path = os.path.join(self.tmp, "kopia.tar.gz")
        with open(archive_path, "w") as f:
            f.write("fake archive bytes")

        mock_run = MagicMock()
        infra.hook_install_kopia_binary({"KOPIA_ARCH": "arm64"}, self.tmp, archive_path, mock_run)

        target_dir = os.path.join(self.tmp, "usr", "bin")
        self.assertEqual(mock_run.call_count, 2)
        extract_cmd = mock_run.call_args_list[0][0][0]
        self.assertIn("tar", extract_cmd)
        self.assertIn(target_dir, extract_cmd)
        # Real bug found and fixed 2026-09-13 hardware-qualifying pi500-1 (a real
        # Raspberry Pi 500, aarch64): this internal tar-extraction path used to be
        # hardcoded to "kopia-0.23.1-linux-x64/kopia" regardless of the real machine
        # architecture -- tar/chmod both "succeed" against the wrong-architecture
        # binary (they don't inspect ELF headers), so the bug was silent until kopia
        # was actually invoked later, when it failed with "Exec format error". Never
        # caught on the dev box since it's genuinely x86_64. This asserts the real
        # fix: the extracted path name matches the real (here, mocked) architecture.
        self.assertIn("kopia-0.23.1-linux-arm64/kopia", extract_cmd)
        chmod_cmd = mock_run.call_args_list[1][0][0]
        self.assertEqual(chmod_cmd, ["chmod", "+x", os.path.join(target_dir, "kopia")])
        self.assertFalse(os.path.exists(archive_path))

    def test_a_run_cmd_failure_is_swallowed_and_the_archive_is_still_cleaned_up(self):
        archive_path = os.path.join(self.tmp, "kopia.tar.gz")
        with open(archive_path, "w") as f:
            f.write("fake archive bytes")
        mock_run = MagicMock(side_effect=RuntimeError("simulated tar failure"))
        infra.hook_install_kopia_binary(
            {"KOPIA_ARCH": "arm64"}, self.tmp, archive_path, mock_run
        )  # must not raise
        self.assertFalse(os.path.exists(archive_path))
        # Bug-hunt fix (2026-09-18, night_shift_todo
        # provisioning-silent-hook-failures-summary-328e3e45.md): swallowing the
        # failure used to be the end of the story -- provisioning reported plain
        # success even with no kopia binary installed. Now the failure must also
        # be recorded for the end-of-run summary, not just logged and forgotten.
        failures = infra.get_hook_failures()
        self.assertEqual(len(failures), 1)
        name, msg = failures[0]
        self.assertEqual(name, "hook_install_kopia_binary")
        self.assertIn("simulated tar failure", msg)
        summary = infra.render_hook_failure_summary()
        self.assertIn("DEGRADED", summary)
        self.assertIn("hook_install_kopia_binary", summary)

    def test_an_unresolvable_architecture_is_swallowed_too_not_raised(self):
        # hook_install_kopia_binary's own try/except covers _kopia_release_arch's
        # deliberate RuntimeError for an unmapped architecture too, same as any other
        # failure in this best-effort install -- it logs and cleans up rather than
        # crashing the rest of provisioning over an optional backup tool.
        archive_path = os.path.join(self.tmp, "kopia.tar.gz")
        with open(archive_path, "w") as f:
            f.write("fake archive bytes")
        infra.hook_install_kopia_binary(
            {"DEB_TARGET_ARCH_CPU": "riscv64"}, self.tmp, archive_path, MagicMock()
        )  # must not raise
        self.assertFalse(os.path.exists(archive_path))


class HookInstallWkhtmltopdfTests(_TmpDirTestCase):
    # Real production failure, found live 2026-09-29: "Unable to find Wkhtmltopdf on this system"
    # -- Debian dropped the wkhtmltopdf package from its own repos entirely, so Odoo's own PDF
    # reports (e.g. Event: Registration Confirmation's attached ticket) failed outright. Odoo's
    # own docs point at wkhtmltopdf's own GitHub releases instead of a distro package.
    def test_installs_the_downloaded_deb_via_apt_get_so_its_own_deps_resolve(self):
        deb_path = os.path.join(self.tmp, "wkhtmltox.deb")
        with open(deb_path, "w") as f:
            f.write("fake deb bytes")
        mock_run = MagicMock()
        infra.hook_install_wkhtmltopdf({}, self.tmp, deb_path, mock_run)
        # apt-get install (not a bare dpkg -i) so the package's own declared Depends
        # (xfonts-75dpi, xfonts-base, and friends) resolve automatically instead of being
        # left missing, exactly as verified against the real production install.
        mock_run.assert_called_once_with(["apt-get", "install", "-y", deb_path])
        self.assertFalse(os.path.exists(deb_path))

    def test_a_run_cmd_failure_is_swallowed_and_the_deb_is_still_cleaned_up(self):
        deb_path = os.path.join(self.tmp, "wkhtmltox.deb")
        with open(deb_path, "w") as f:
            f.write("fake deb bytes")
        mock_run = MagicMock(side_effect=RuntimeError("simulated apt failure"))
        infra.hook_install_wkhtmltopdf({}, self.tmp, deb_path, mock_run)  # must not raise
        self.assertFalse(os.path.exists(deb_path))
        failures = infra.get_hook_failures()
        self.assertEqual(len(failures), 1)
        name, msg = failures[0]
        self.assertEqual(name, "hook_install_wkhtmltopdf")
        self.assertIn("simulated apt failure", msg)
        summary = infra.render_hook_failure_summary()
        self.assertIn("DEGRADED", summary)
        self.assertIn("hook_install_wkhtmltopdf", summary)


class HookFailureSummaryTests(_TmpDirTestCase):
    """Tests for the end-of-run hook-failure summary mechanism itself
    (night_shift_todo provisioning-silent-hook-failures-summary-328e3e45.md):
    non-fatal hook failures must be collected across a run and surfaced in
    one prominent summary, without aborting the run for any single one."""

    def test_two_failing_hooks_both_appear_in_the_end_of_run_summary(self):
        archive_path = os.path.join(self.tmp, "kopia.tar.gz")
        with open(archive_path, "w") as f:
            f.write("fake archive bytes")
        infra.hook_install_kopia_binary(
            {"KOPIA_ARCH": "arm64"},
            self.tmp,
            archive_path,
            MagicMock(side_effect=RuntimeError("simulated tar failure")),
        )

        ssl_dir = os.path.join(self.tmp, "ssl")
        os.makedirs(ssl_dir, exist_ok=True)
        infra.hook_generate_ssl(
            {},
            self.tmp,
            ssl_dir,
            MagicMock(side_effect=RuntimeError("simulated openssl failure")),
        )

        failures = infra.get_hook_failures()
        names = [name for name, _ in failures]
        self.assertEqual(len(failures), 2)
        self.assertIn("hook_install_kopia_binary", names)
        self.assertIn("hook_generate_ssl", names)

        summary = infra.render_hook_failure_summary()
        self.assertIn("DEGRADED", summary)
        self.assertIn("hook_install_kopia_binary", summary)
        self.assertIn("hook_generate_ssl", summary)
        self.assertIn("simulated tar failure", summary)
        self.assertIn("simulated openssl failure", summary)

        self.assertTrue(infra.print_hook_failure_summary())

    def test_a_clean_run_with_no_hook_failures_produces_no_degraded_marker(self):
        self.assertEqual(infra.get_hook_failures(), [])
        self.assertIsNone(infra.render_hook_failure_summary())
        self.assertFalse(infra.print_hook_failure_summary())

    def test_reset_hook_failures_clears_prior_recordings(self):
        infra.record_hook_failure("some_hook", RuntimeError("boom"))
        self.assertEqual(len(infra.get_hook_failures()), 1)
        infra.reset_hook_failures()
        self.assertEqual(infra.get_hook_failures(), [])
        self.assertIsNone(infra.render_hook_failure_summary())


class ExecuteHooksTests(_TmpDirTestCase):
    """execute_hooks() is the dispatcher that runs MANIFEST["directories"]
    entries' post_provision_hooks for a given environment. Bug found
    2026-09-18: this function existed and was exercised in isolation by
    nothing, but was never actually called from provision_environment (or
    anywhere else in real provisioning) -- apply_production_directories()
    creates the directories but never runs their hooks, so hook_generate_ssl
    and hook_clear_pycache silently never ran during a real provisioning
    run. These tests call the real execute_hooks() (not a mock of it) with a
    mocked run_cmd_func, matching HookGenerateSslTests'/HookClearPycacheTests'
    own boundary, and assert the real, observable effects of the hooks it
    dispatches to -- proving the dispatch itself works, not just the hooks
    in isolation (already covered above)."""

    def test_prod_environment_actually_generates_ssl_certs_and_clears_pycache(self):
        ssl_dir = os.path.join(self.tmp, "opt/hams/nginx/ssl")
        pycache_dir = os.path.join(self.tmp, "opt/hams/pycache")
        os.makedirs(ssl_dir)
        os.makedirs(pycache_dir)
        with open(os.path.join(pycache_dir, "stale.pyc"), "w") as f:
            f.write("x")

        fullchain = os.path.join(ssl_dir, "fullchain.pem")

        # **kwargs: a real run_cmd_func accepts stdin=... too (hook_create_
        # pdns_sqlite_schema's own real invocation passes it) -- since the
        # /var/lib/powerdns MANIFEST["directories"] entry (moved there from
        # "static_files", 2026-09-23 -- see that entry's own comment) now
        # correctly reaches execute_hooks() too, this fake needs to accept
        # the same call shape a real one would.
        def fake_run_cmd(cmd, **kwargs):
            if cmd[:1] == ["openssl"]:
                with open(fullchain, "w") as f:
                    f.write("cert")
                with open(os.path.join(ssl_dir, "privkey.pem"), "w") as f:
                    f.write("key")

        mock_run = MagicMock(side_effect=fake_run_cmd)

        infra.execute_hooks("prod", mock_run, {"DOMAIN": "hams.com"}, dest_dir=self.tmp)

        # hook_generate_ssl really ran, against the real, correctly
        # dest_dir-joined MANIFEST directory path.
        openssl_calls = [c for c in mock_run.call_args_list if c.args[0][:1] == ["openssl"]]
        self.assertEqual(len(openssl_calls), 1)
        self.assertTrue(os.path.exists(os.path.join(ssl_dir, "lotw_root.pem")))

        # hook_clear_pycache really ran too.
        self.assertEqual(os.listdir(pycache_dir), [])

        # hook_create_pdns_sqlite_schema now really runs too, since its own
        # MANIFEST entry moved from "static_files" (which execute_hooks()
        # never iterated at all) to "directories" (which it does) -- the
        # real gap this whole move exists to close (see that entry's own
        # comment: "nothing in this codebase ever created pdns.sqlite3").
        sqlite_calls = [c for c in mock_run.call_args_list if c.args[0][:1] == ["sqlite3"]]
        self.assertEqual(len(sqlite_calls), 1)
        self.assertIn(
            os.path.join(self.tmp, "var/lib/powerdns/pdns.sqlite3"),
            sqlite_calls[0].args[0],
        )
        self.assertIn("stdin", sqlite_calls[0].kwargs)

    def test_an_environment_with_no_hooked_directories_runs_no_hooks(self):
        mock_run = MagicMock()
        infra.execute_hooks("nonexistent_env", mock_run, {}, dest_dir=self.tmp)
        mock_run.assert_not_called()


class KopiaReleaseArchTests(_SafePatchTestCase):
    def test_maps_known_debian_architectures_to_kopias_own_asset_names(self):
        self.assertEqual(infra._kopia_release_arch({"DEB_TARGET_ARCH_CPU": "amd64"}), "x64")
        self.assertEqual(infra._kopia_release_arch({"DEB_TARGET_ARCH_CPU": "arm64"}), "arm64")
        self.assertEqual(infra._kopia_release_arch({"DEB_TARGET_ARCH_CPU": "armhf"}), "arm")

    def test_raises_rather_than_guessing_for_an_unknown_architecture(self):
        # Real bug this whole function exists to close: silently falling back to
        # "x64" (or any other guess) for an architecture kopia doesn't publish a
        # release for would just reproduce the original wrong-binary bug under a
        # different name. Fail loudly instead.
        with self.assertRaises(RuntimeError):
            infra._kopia_release_arch({"DEB_TARGET_ARCH_CPU": "riscv64"})

    def test_resolves_deb_target_arch_cpu_itself_when_not_already_set(self):
        env_vars = {}
        mock_run = self.safe_patch_object(
            infra.subprocess, "run",
            return_value=MagicMock(returncode=0, stdout="arm64\n"),
        )
        result = infra._kopia_release_arch(env_vars)
        mock_run.assert_called_once_with(
            ["dpkg-architecture", "-q", "DEB_TARGET_ARCH_CPU"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result, "arm64")
        self.assertEqual(env_vars["DEB_TARGET_ARCH_CPU"], "arm64")


class HookDaemonsPermsTests(_TmpDirTestCase):
    def test_chowns_and_chmods_when_the_target_exists(self):
        target = os.path.join(self.tmp, "daemons")
        os.makedirs(target)
        mock_run = MagicMock()
        infra.hook_daemons_perms({}, "", target, mock_run)
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(mock_run.call_args_list[0][0][0], ["chown", "-R", "hams_com:hams_com", target])
        self.assertEqual(mock_run.call_args_list[1][0][0], ["chmod", "-R", "a+rX", target])

    def test_does_nothing_when_the_target_does_not_exist(self):
        mock_run = MagicMock()
        infra.hook_daemons_perms({}, "", os.path.join(self.tmp, "nope"), mock_run)
        mock_run.assert_not_called()


class ServiceStartCommandTests(unittest.TestCase):
    """_service_start_command() is the one piece of run_post_provision_
    smoketest()'s own logic worth testing in isolation (see that function's
    own module-level NON_BLOCKING_START_SERVICES comment for the real
    2026-09-24 production incident this fixes) -- pure, no subprocess/host
    interaction, matching this file's own established reasoning for why the
    smoketest function itself isn't attempted here."""

    def test_callbook_geo_enrich_gets_no_block(self):
        self.assertEqual(
            infra._service_start_command("callbook.geo.enrich.service"),
            ["systemctl", "start", "--no-block", "callbook.geo.enrich.service"],
        )

    def test_an_ordinary_service_stays_blocking(self):
        # fcc.uls.sync.service and hamcall.idx.sync.service are the two other
        # real one-shot sync services in the same smoketest loop -- both fail
        # or finish in seconds (confirmed from the real 2026-09-24 incident
        # log), so they must NOT pick up --no-block just because they sit next
        # to callbook.geo.enrich.service in the same MANIFEST/loop.
        for svc in ("fcc.uls.sync.service", "hamcall.idx.sync.service", "odoo", "postgresql"):
            with self.subTest(svc=svc):
                self.assertEqual(
                    infra._service_start_command(svc),
                    ["systemctl", "start", svc],
                )

    def test_non_blocking_start_services_contains_exactly_callbook_geo_enrich(self):
        # Guards against someone widening the exception set casually --
        # today it should be exactly this one service, not a growing list.
        self.assertEqual(
            infra.NON_BLOCKING_START_SERVICES, {"callbook.geo.enrich.service"}
        )


class GenerateSecurePasswordTests(unittest.TestCase):
    def test_default_length_is_32_characters(self):
        self.assertEqual(len(infra.generate_secure_password()), 32)

    def test_a_custom_length_is_honored(self):
        self.assertEqual(len(infra.generate_secure_password(16)), 16)

    def test_two_calls_produce_different_passwords(self):
        # Not a cryptographic proof, just confirms this isn't a fixed
        # constant or a deterministic-seed bug.
        self.assertNotEqual(infra.generate_secure_password(), infra.generate_secure_password())

    def test_only_uses_letters_and_digits(self):
        pw = infra.generate_secure_password(200)
        allowed = set(infra.string.ascii_letters + infra.string.digits)
        self.assertTrue(set(pw) <= allowed)


class GetMountPathsTests(unittest.TestCase):
    def test_filters_by_environment_and_runtime_mount_type(self):
        fake_manifest = {
            "directories": [
                {"path": "/a", "environments": ["prod"], "runtime_mount": "bind"},
                {"path": "/b", "environments": ["prod", "test"], "runtime_mount": "bind"},
                {"path": "/c", "environments": ["prod"], "runtime_mount": "tmpfs"},
                {"path": "/d", "environments": ["test"], "runtime_mount": "bind"},
            ]
        }
        with patch.dict(infra.MANIFEST, fake_manifest, clear=True):
            self.assertEqual(sorted(infra.get_mount_paths("prod", "bind")), ["/a", "/b"])
            self.assertEqual(infra.get_mount_paths("test", "bind"), ["/b", "/d"])
            self.assertEqual(infra.get_mount_paths("prod", "tmpfs"), ["/c"])
            self.assertEqual(infra.get_mount_paths("test", "tmpfs"), [])


def _mode_at_first_write(tmp, target_path, real_open, action):
    """
    Runs `action()` (expected to os.open() + write to target_path, which
    must already exist on disk) and returns the file's permission bits at
    the exact moment the first write-mode `open(fd, ...)` call is made on
    it -- i.e. whether the file was already hardened to its final mode
    *before* any content was written, or only after
    (`apply_permissions()`/a trailing chmod runs later).
    """
    captured = {}

    def spying_open(fd_or_path, *a, **kw):
        f = real_open(fd_or_path, *a, **kw)
        if isinstance(fd_or_path, int) and "mode" not in captured:
            captured["mode"] = os.stat(target_path).st_mode & 0o777
        return f

    with patch("builtins.open", side_effect=spying_open):
        action()
    return captured["mode"]


class WriteEnvFilesTests(_TmpDirTestCase):
    def test_creates_new_files_at_mode_400(self):
        infra.write_env_files(self.tmp, {"DB_NAME": "hams_test"}, MagicMock())
        path = os.path.join(self.tmp, "db.env")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o400)

    def test_a_pre_existing_looser_permission_file_is_hardened_before_any_content_is_written(self):
        # Regression test for a real credential-handling gap: os.open()'s
        # own `mode` argument is silently ignored by the kernel when the
        # target file already exists, so re-provisioning over a legacy/
        # loosely-permissioned db.env used to write fresh secret content
        # (DB_PASS, ODOO_ADMIN_PASSWORD, CLOUDFLARE_API_TOKEN, ...) into a
        # file still world/group-readable for the entire duration of the
        # write, only tightened by the trailing apply_permissions() call
        # *after* the secret was already on disk at the old permissions.
        filepath = os.path.join(self.tmp, "db.env")
        with open(filepath, "w") as f:
            f.write("DB_NAME=old\n")
        os.chmod(filepath, 0o644)
        self.assertEqual(os.stat(filepath).st_mode & 0o777, 0o644)

        mode_during_write = _mode_at_first_write(
            self.tmp,
            filepath,
            _REAL_OPEN,
            lambda: infra.write_env_files(
                self.tmp, {"DB_NAME": "hams_test", "POSTGRES_PASSWORD": "s3cr3t"}, MagicMock()
            ),
        )

        self.assertEqual(
            mode_during_write,
            0o400,
            "db.env must already be hardened to 0o400 by the time secret "
            "content is written, not left at its old, looser permissions "
            "until a trailing chmod runs after the write",
        )
        # And the final state is still correct too.
        self.assertEqual(os.stat(filepath).st_mode & 0o777, 0o400)
        with open(filepath) as f:
            self.assertIn("POSTGRES_PASSWORD=s3cr3t", f.read())

    def test_only_writes_keys_present_in_env_vars(self):
        infra.write_env_files(self.tmp, {"DB_NAME": "hams_test"}, MagicMock())
        with open(os.path.join(self.tmp, "db.env")) as f:
            content = f.read()
        self.assertIn("DB_NAME=hams_test", content)
        self.assertNotIn("DB_PASS=", content)


class ProvisionStaticFilesPermissionTests(_TmpDirTestCase):
    def test_a_pre_existing_looser_permission_file_is_hardened_before_content_is_written(self):
        fake_manifest = {
            "static_files": [
                {
                    "path": os.path.join(self.tmp, "secret.conf"),
                    "content": "top-secret-value\n",
                    "owner": None,
                    "mode": "600",
                    "environments": ["prod"],
                }
            ]
        }
        target = os.path.join(self.tmp, "secret.conf")
        with open(target, "w") as f:
            f.write("old\n")
        os.chmod(target, 0o644)

        with patch.dict(infra.MANIFEST, fake_manifest, clear=True):
            mode_during_write = _mode_at_first_write(
                self.tmp,
                target,
                _REAL_OPEN,
                lambda: infra.provision_static_files(MagicMock(), {}, environment="prod"),
            )

        self.assertEqual(mode_during_write, 0o600)
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)

    def test_provision_static_files_raises_loudly_on_an_unresolved_placeholder(self):
        # Companion to FormatEnvTests.test_a_missing_variable_now_raises_instead_of_silently_degrading:
        # confirms the fix actually surfaces end-to-end through provision_static_files(),
        # not just in format_env() in isolation -- a manifest entry referencing a variable
        # missing from env_vars (a typo, or a var some pre-population step failed to set)
        # must fail the provisioning run rather than write a file to disk with a literal,
        # unresolved "{VAR}" in it while reporting success.
        fake_manifest = {
            "static_files": [
                {
                    "path": os.path.join(self.tmp, "broken.conf"),
                    "content": "key={TYPOED_VAR_NAME}\n",
                    "owner": None,
                    "mode": "600",
                    "environments": ["prod"],
                }
            ]
        }
        with patch.dict(infra.MANIFEST, fake_manifest, clear=True):
            with self.assertRaises(KeyError):
                infra.provision_static_files(MagicMock(), {}, environment="prod")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "broken.conf")))


class ProvisionSystemdOverrideTests(_TmpDirTestCase):
    def setUp(self):
        super().setUp()
        self.override_dir = os.path.join(self.tmp, "etc/systemd/system/odoo.service.d")

    def test_a_pre_existing_looser_permission_override_file_is_hardened_before_content_is_written(self):
        os.makedirs(self.override_dir, exist_ok=True)
        override_file = os.path.join(self.override_dir, "override.conf")
        with open(override_file, "w") as f:
            f.write("old\n")
        os.chmod(override_file, 0o666)

        mode_during_write = _mode_at_first_write(
            self.tmp,
            override_file,
            _REAL_OPEN,
            lambda: infra.provision_systemd_override(MagicMock(), {}, environment="prod", dest_dir=self.tmp),
        )

        self.assertEqual(mode_during_write, 0o644)
        self.assertEqual(os.stat(override_file).st_mode & 0o777, 0o644)

    def test_pdns_override_writes_to_its_own_unit_dir_with_cleared_execstart(self):
        # Generalized 2026-09-22 to also cover pdns.service (see
        # systemd_pdns_override's own comment) -- this pins that the
        # manifest_key/unit_name parameters actually route to a distinct
        # file, and that ExecStart's two-item list produces the
        # clear-then-set idiom systemd requires to override rather than
        # append to a packaged unit's own ExecStart=.
        infra.provision_systemd_override(
            MagicMock(),
            {},
            environment="prod",
            dest_dir=self.tmp,
            manifest_key="systemd_pdns_override",
            unit_name="pdns",
        )
        override_file = os.path.join(
            self.tmp, "etc/systemd/system/pdns.service.d/override.conf"
        )
        self.assertTrue(os.path.exists(override_file))
        with open(override_file) as f:
            content = f.read()
        lines = content.splitlines()
        self.assertIn("[Service]", lines)
        execstart_lines = [ln for ln in lines if ln.startswith("ExecStart=")]
        self.assertEqual(execstart_lines[0], "ExecStart=")
        self.assertIn("--config-name=gsqlite3", execstart_lines[1])
        # The odoo override must be untouched by this call.
        self.assertFalse(
            os.path.exists(
                os.path.join(self.tmp, "etc/systemd/system/odoo.service.d/override.conf")
            )
        )

    def test_odoo_override_restarts_on_failure(self):
        # Real incident, 2026-10-01: odoo.service had no Restart= at all, so a
        # transient PostgreSQL restart (check_registries()'s own pre-fork
        # health check opening a cursor mid-restart, with no retry) crashed
        # the whole server process -- and systemd just left it "failed"
        # rather than bringing it back, a real site outage twice in one
        # night until someone noticed and ran `systemctl start odoo` by
        # hand. See this file's own comment at the manifest entry.
        infra.provision_systemd_override(MagicMock(), {}, environment="prod", dest_dir=self.tmp)
        override_file = os.path.join(self.override_dir, "override.conf")
        with open(override_file) as f:
            lines = f.read().splitlines()
        self.assertIn("Restart=on-failure", lines)
        self.assertIn("RestartSec=5", lines)


class ProvisionModeEnvFileTests(_TmpDirTestCase):
    """load_and_prompt_env() must not carry saved *.env values across test-mode and production
    provisioning (night_shift_todo provisioning-test-mode-env-dir-leak). Real files in a temporary
    directory stand in for /opt/hams/etc: glob is pointed at them, so the reader, the setdefault
    ordering and the mode marker all run for real."""

    def setUp(self):
        super().setUp()
        self.safe_patch("infrastructure.os.path.exists", return_value=True)
        real_glob = infra.glob.glob
        self.safe_patch(
            "infrastructure.glob.glob",
            side_effect=lambda pattern: real_glob(os.path.join(self.tmp, "*.env")),
        )
        os.environ.pop("HAMS_ALLOW_PROVISION_MODE_SWITCH", None)

    def _write(self, name, text):
        with open(os.path.join(self.tmp, name), "w") as f:
            f.write(text)

    def _prod_files(self):
        self._write("db.env", "DB_NAME=hams_prod\nDB_PASS=prod-generated-secret\nDB_HOST=prod-db\n")
        self._write("core.env", "DOMAIN=hams.com\nHAMS_PROVISION_MODE=prod\n")

    def test_a_test_run_refuses_env_files_saved_by_production(self):
        # Tests [@ANCHOR: infrastructure:_refuse_env_files_from_other_provision_mode]
        self._prod_files()
        with self.assertRaisesRegex(RuntimeError, "HAMS_ALLOW_PROVISION_MODE_SWITCH"):
            infra.load_and_prompt_env({}, is_test=True)

    def test_an_explicit_switch_to_test_uses_test_defaults_not_production_values(self):
        self._prod_files()
        env_vars = {}
        with patch.dict(os.environ, {"HAMS_ALLOW_PROVISION_MODE_SWITCH": "1"}):
            infra.load_and_prompt_env(env_vars, is_test=True)
        self.assertEqual(env_vars["DB_NAME"], "hams_test")
        self.assertEqual(env_vars["DB_PASS"], "odoo")
        self.assertEqual(env_vars["DB_HOST"], "postgres")
        self.assertNotEqual(env_vars["DOMAIN"], "hams.com")
        self.assertEqual(env_vars["HAMS_PROVISION_MODE"], "test")

    def test_a_production_run_refuses_env_files_saved_by_a_test_run(self):
        self._write("db.env", "DB_NAME=hams_test\nDB_PASS=odoo\n")
        self._write("odoo.env", "ODOO_ADMIN_PASSWORD=admin\n")
        self._write("core.env", "DOMAIN=test.invalid\nHAMS_PROVISION_MODE=test\n")
        with self.assertRaisesRegex(RuntimeError, "'test' mode"):
            infra.load_and_prompt_env({"DOMAIN": "hams.com"}, is_test=False)

        env_vars = {"DOMAIN": "hams.com"}
        with patch.dict(os.environ, {"HAMS_ALLOW_PROVISION_MODE_SWITCH": "1"}):
            infra.load_and_prompt_env(env_vars, is_test=False)
        self.assertEqual(env_vars["DB_NAME"], "hams_prod")
        self.assertNotEqual(env_vars["DB_PASS"], "odoo")
        self.assertNotEqual(env_vars["ODOO_ADMIN_PASSWORD"], "admin")
        self.assertEqual(env_vars["HAMS_PROVISION_MODE"], "prod")

    def test_same_mode_reprovisioning_keeps_saved_values(self):
        self._prod_files()
        env_vars = {}
        infra.load_and_prompt_env(env_vars, is_test=False)
        self.assertEqual(env_vars["DB_PASS"], "prod-generated-secret")
        self.assertEqual(env_vars["DOMAIN"], "hams.com")
        self.assertEqual(env_vars["HAMS_PROVISION_MODE"], "prod")

    def test_files_from_before_the_marker_existed_are_still_read(self):
        self._write("db.env", "DB_NAME=legacy_db\n")
        env_vars = {}
        infra.load_and_prompt_env(env_vars, is_test=True)
        self.assertEqual(env_vars["DB_NAME"], "legacy_db")
        self.assertEqual(env_vars["HAMS_PROVISION_MODE"], "test")

    def test_the_mode_is_persisted_to_core_env(self):
        env_vars = {}
        infra.load_and_prompt_env(env_vars, is_test=True)
        out = os.path.join(self.tmp, "written")
        self.safe_patch("infrastructure.apply_permissions")
        infra.write_env_files(out, env_vars, MagicMock())
        with open(os.path.join(out, "core.env")) as f:
            self.assertIn("HAMS_PROVISION_MODE=test\n", f.read())


class LoadAndPromptEnvTests(_SafePatchTestCase):
    """load_and_prompt_env() no longer prompts interactively -- these confirm
    the replacement non-interactive contract: DOMAIN has no safe default and
    must fail fast rather than silently guess or block on stdin; every other
    previously-prompted value falls back to its old default unattended;
    ODOO_ADMIN_PASSWORD is generated like the other secrets, not left for a
    human to type in. Patches os.path.exists to False so these never touch
    the real box's /opt/hams/etc."""

    def setUp(self):
        super().setUp()
        self.safe_patch("infrastructure.os.path.exists", return_value=False)

    def test_raises_when_domain_is_missing(self):
        with self.assertRaisesRegex(RuntimeError, "DOMAIN"):
            infra.load_and_prompt_env({}, is_test=False)

    def test_does_not_raise_when_domain_is_supplied(self):
        env_vars = {"DOMAIN": "hams.com"}
        infra.load_and_prompt_env(env_vars, is_test=False)
        self.assertEqual(env_vars["DOMAIN"], "hams.com")

    def test_test_mode_never_raises_even_without_domain(self):
        env_vars = {}
        infra.load_and_prompt_env(env_vars, is_test=True)
        self.assertEqual(env_vars["DOMAIN"], "localhost")

    def test_generates_odoo_admin_password_when_missing(self):
        env_vars = {"DOMAIN": "hams.com"}
        infra.load_and_prompt_env(env_vars, is_test=False)
        self.assertEqual(len(env_vars["ODOO_ADMIN_PASSWORD"]), 32)

    def test_preserves_a_supplied_odoo_admin_password(self):
        env_vars = {"DOMAIN": "hams.com", "ODOO_ADMIN_PASSWORD": "already-set"}
        infra.load_and_prompt_env(env_vars, is_test=False)
        self.assertEqual(env_vars["ODOO_ADMIN_PASSWORD"], "already-set")

    def test_previously_prompted_values_fall_back_to_their_old_defaults(self):
        env_vars = {"DOMAIN": "hams.com"}
        infra.load_and_prompt_env(env_vars, is_test=False)
        # Bruce, 2026-09-22: hams.com sends outbound mail through Amazon SES, not
        # Mailgun -- the old smtp.mailgun.org default was stale from an earlier
        # provider and never actually configured deliberately. SES's SMTP endpoint
        # is region-specific; AWS_REGION defaults to us-east-1 to match
        # daemons/ses_inbound_mail_ingest's own default for the same account.
        self.assertEqual(env_vars["AWS_REGION"], "us-east-1")
        self.assertEqual(env_vars["SMTP_HOST"], "email-smtp.us-east-1.amazonaws.com")
        self.assertEqual(env_vars["SMTP_PORT"], "587")
        # SES authenticates with an IAM access key id (as SMTP_USER), not an email
        # address -- "none" fails closed exactly as it did for the old default.
        self.assertEqual(env_vars["SMTP_USER"], "none")
        self.assertEqual(env_vars["SMTP_PASS"], "none")
        self.assertEqual(env_vars["GEMINI_API_KEY"], "none")
        self.assertEqual(env_vars["GEMINI_MODEL"], "gemini-2.5-pro")
        self.assertEqual(env_vars["CLOUDFLARE_API_TOKEN"], "none")
        self.assertEqual(env_vars["CLOUDFLARE_ZONE_ID"], "none")
        self.assertEqual(env_vars["CLOUDFLARE_TUNNEL_TOKEN"], "none")

    def test_cloudflare_zone_id_derivation_failure_is_logged_and_falls_back_to_none(self):
        # Regression test for the narrow catch-all-exception fix: a real API
        # token is supplied (so the urllib.request.urlopen() branch actually
        # runs) and urlopen is mocked to raise, simulating a real-world
        # failure mode (DNS/network error, HTTP error, malformed JSON, etc).
        # This must not crash load_and_prompt_env -- Zone ID derivation is
        # best-effort -- but the failure must now be both printed (existing
        # behavior) and logged via _logger.warning (added so the
        # `# audit-ignore-catch-all` handler satisfies check_burn_list.py's
        # own "must log or re-raise" requirement instead of silently
        # swallowing the traceback).
        env_vars = {"DOMAIN": "hams.com", "CLOUDFLARE_API_TOKEN": "a-real-token"}
        with patch(
            "urllib.request.urlopen",
            side_effect=OSError("simulated network failure"),
        ), self.assertLogs("infrastructure", level="WARNING") as log_ctx:
            infra.load_and_prompt_env(env_vars, is_test=False)
        self.assertEqual(env_vars["CLOUDFLARE_ZONE_ID"], "none")
        self.assertTrue(
            any("Failed to fetch Cloudflare Zone ID" in msg for msg in log_ctx.output)
        )

    def test_never_reads_stdin(self):
        # A regression guard for the exact bug class being removed: nothing
        # in load_and_prompt_env should call input() or getpass.getpass()
        # unattended, which would hang a headless provisioning run forever.
        env_vars = {"DOMAIN": "hams.com"}
        with patch("builtins.input", side_effect=AssertionError("must not prompt")):
            infra.load_and_prompt_env(env_vars, is_test=False)
        # A regression guard checking whether a specific name is bound in the infra
        # module's own namespace, not a production-code probe for an uncertain interface.
        self.assertFalse(hasattr(infra, "getpass"))  # burn-ignore-introspection


class CreateOdooRoleIfMissingTests(_SafePatchTestCase):
    def _run(self, db_pass, role_already_exists):
        mock_role_exists = self.safe_patch_object(
            infra, "_role_exists", return_value=role_already_exists
        )
        mock_run_cmd_func = MagicMock()
        infra._create_odoo_role_if_missing(mock_run_cmd_func, db_pass)
        mock_role_exists.assert_called_once_with("odoo")
        return mock_run_cmd_func

    def test_a_single_quote_in_db_pass_does_not_reach_the_sql_text_unescaped(self):
        # Regression test for a real SQL-injection bug: an older version of
        # this code f-string-interpolated db_pass directly into
        # `PASSWORD '{db_pass}'`, so a password containing a single quote
        # broke out of the SQL string literal and injected arbitrary SQL,
        # executed as the postgres superuser via `sudo -u postgres`.
        malicious_pass = "x'; DROP TABLE pg_roles; --"
        mock_run_cmd_func = self._run(malicious_pass, role_already_exists=False)

        cmd = mock_run_cmd_func.call_args[0][0]
        sql_text = mock_run_cmd_func.call_args.kwargs["input"]
        self.assertNotIn(malicious_pass, sql_text)
        self.assertIn(":'db_pass'", sql_text)

        # The raw value is instead passed via psql's own `-v` mechanism,
        # which the SQL text references as `:'db_pass'` -- psql itself
        # (not this code) applies SQL-literal quoting at expansion time.
        v_value = cmd[cmd.index("-v") + 1]
        self.assertEqual(v_value, f"db_pass={malicious_pass}")

    def test_normal_password_still_produces_a_working_command_shape(self):
        cmd = self._run("normalPass123", role_already_exists=False).call_args[0][0]
        self.assertEqual(cmd[:4], ["sudo", "-u", "postgres", "psql"])
        self.assertIn("db_pass=normalPass123", cmd)

    def test_role_creation_sql_is_not_wrapped_in_a_dollar_quoted_do_block(self):
        # Real bug found and fixed 2026-09-13 hardware-qualifying pi500-1 (a
        # genuinely fresh Raspberry Pi 500): the prior version wrapped this
        # CREATE ROLE in a `DO $$...$$` PL/pgSQL block so the SQL itself
        # could check IF NOT EXISTS -- but psql's own `:'variable'`
        # substitution does not apply inside a dollar-quoted string body,
        # so `PASSWORD :'db_pass'` reached the server as the literal,
        # unsubstituted text, which PostgreSQL's parser rejected outright
        # with "syntax error at or near ':'" -- breaking role creation on
        # every genuinely fresh box. Never caught on the dev box because
        # its own `odoo` role already existed from unrelated prior history.
        # This asserts the real fix: no dollar-quoting anywhere in the SQL.
        call = self._run("normalPass123", role_already_exists=False).call_args
        sql_text = call.kwargs["input"]
        self.assertNotIn("$$", sql_text, f"SQL must not use dollar-quoting: {sql_text!r}")
        self.assertNotIn("DO ", sql_text, f"SQL must not be a DO block: {sql_text!r}")
        self.assertIn("CREATE ROLE odoo", sql_text)

    def test_role_creation_sql_is_not_passed_via_dash_c(self):
        # Real bug found and fixed 2026-09-13 hardware-qualifying pi500-1,
        # discovered only AFTER the dollar-quoting fix above still failed
        # identically on a real, genuinely fresh Postgres 18 instance:
        # psql's `:'variable'` substitution does not apply at all when SQL
        # is passed via `-c` (confirmed directly with a bare
        # `psql -v x=hello -c "SELECT :'x';"`, which fails with the
        # identical syntax error the dollar-quoting bug produced, on both a
        # real Raspberry Pi 500 and the x86_64 dev box, same psql version)
        # -- only when the SQL is read as a script over stdin. This asserts
        # the real, complete fix: -c is gone from the argv, the SQL goes
        # over stdin (`input=`) instead.
        call = self._run("normalPass123", role_already_exists=False).call_args
        cmd = call.args[0]
        self.assertNotIn("-c", cmd, f"SQL must not be passed via -c: {cmd!r}")
        self.assertIn("input", call.kwargs, "SQL must be piped over stdin instead")

    def test_existing_role_is_not_recreated_but_is_granted_superuser(self):
        # hams_shared PR #66 (d31aad3, found live provisioning jetson-1): the
        # odoo apt package's postinst pre-creates a NON-superuser `odoo` role,
        # so the already-exists path must still ALTER it to SUPERUSER (needed
        # for `CREATE EXTENSION vector`). Exactly one command runs: that
        # ALTER ROLE -- no CREATE ROLE, and db_pass never reaches the argv.
        mock_run_cmd_func = self._run("normalPass123", role_already_exists=True)
        mock_run_cmd_func.assert_called_once_with(
            ["sudo", "-u", "postgres", "psql", "-c", "ALTER ROLE odoo WITH SUPERUSER;"]
        )
        call = mock_run_cmd_func.call_args
        self.assertNotIn("input", call.kwargs, "the existing-role path must not pipe CREATE ROLE SQL")
        self.assertFalse(
            any("normalPass123" in arg for arg in call.args[0]),
            "db_pass must not be passed when the role already exists",
        )


class ProvisionCacheManagerRoleTests(_TmpDirTestCase):
    """Tests [@ANCHOR: infrastructure:_provision_cache_manager_role]

    night_shift_todo/low/cache-manager-odoo-password-fallback-0ea55461.md: no automated
    provisioning flow ever called scripts/provision_cache_manager_db_role.py, so cache_manager.py's
    own "odoo"/"odoo" fallback was the normal case on every deployment, not an edge case. This is
    the automated-provisioning half of that fix -- see cache_manager.py's own _require_db_credentials
    for the daemon-side half that refuses to start without it.
    """

    def _env_path(self):
        return os.path.join(self.tmp, "keys", "cache_manager_db.env")

    def test_creates_when_the_role_is_absent_and_alters_when_present(self):
        run_cmd = MagicMock()
        self.safe_patch_object(infra, "_role_exists", return_value=False)
        self.safe_patch_object(infra, "apply_permissions")
        infra._provision_cache_manager_role(run_cmd, "hams_test", env_file=self._env_path())
        create_sql = run_cmd.call_args_list[0].kwargs["input"]
        self.assertIn("CREATE ROLE", create_sql)
        self.assertNotIn("ALTER ROLE", create_sql)

        run_cmd.reset_mock()
        self.safe_patch_object(infra, "_role_exists", return_value=True)
        infra._provision_cache_manager_role(run_cmd, "hams_test", env_file=self._env_path())
        alter_sql = run_cmd.call_args_list[0].kwargs["input"]
        self.assertIn("ALTER ROLE", alter_sql)
        self.assertNotIn("CREATE ROLE", alter_sql)

    def test_never_splices_role_name_db_name_or_password_into_the_sql_text(self):
        # Same SQL-injection class already fixed once for _create_odoo_role_if_missing
        # (see that test class's own docstring) -- role_name/db_name go through psql's
        # :"var" identifier substitution, the generated password through :'var' literal
        # substitution, neither ever f-string-interpolated directly into the SQL text.
        run_cmd = MagicMock()
        self.safe_patch_object(infra, "_role_exists", return_value=False)
        self.safe_patch_object(infra, "apply_permissions")
        malicious_db = 'x"; DROP DATABASE hams_prod; --'
        infra._provision_cache_manager_role(
            run_cmd, malicious_db, role_name="cache_manager_ro", env_file=self._env_path()
        )
        role_sql = run_cmd.call_args_list[0].kwargs["input"]
        grant_sql = run_cmd.call_args_list[1].kwargs["input"]
        self.assertIn(':"role_name"', role_sql)
        self.assertIn(":'db_pass'", role_sql)
        self.assertIn(':"db_name"', grant_sql)
        self.assertIn(':"role_name"', grant_sql)
        self.assertNotIn(malicious_db, grant_sql)
        for call in run_cmd.call_args_list:
            self.assertNotIn("-c", call.args[0])

    def test_grant_is_connect_only_no_table_privilege(self):
        run_cmd = MagicMock()
        self.safe_patch_object(infra, "_role_exists", return_value=False)
        self.safe_patch_object(infra, "apply_permissions")
        infra._provision_cache_manager_role(run_cmd, "hams_test", env_file=self._env_path())
        grant_sql = run_cmd.call_args_list[1].kwargs["input"]
        self.assertIn("GRANT CONNECT ON DATABASE", grant_sql)
        self.assertNotIn("ALL PRIVILEGES", grant_sql)
        self.assertNotIn("SELECT", grant_sql)

    def test_writes_an_owner_only_env_file_and_hands_ownership_to_odoo(self):
        # Real bug found and fixed while writing this function: os.open()'s own mode
        # argument only ever governs a brand-new file's initial permissions, and this
        # function's own author (running as root during provisioning) never actually
        # verified the daemon's own OS user (odoo) could read the result -- it could
        # not, since the file was left root:root while the daemon's own load_dotenv()
        # call runs as the unprivileged odoo user (unlike cache_manager.env, loaded by
        # systemd's EnvironmentFile= as root before it drops to User=odoo). Caught by
        # actually running this function end-to-end against a real local Postgres
        # instance and reading the result back as the odoo user, not by this mocked
        # test alone -- this test pins the fix so it can't silently regress.
        run_cmd = MagicMock()
        self.safe_patch_object(infra, "_role_exists", return_value=False)
        mock_apply_permissions = self.safe_patch_object(infra, "apply_permissions")
        env_path = self._env_path()

        infra._provision_cache_manager_role(run_cmd, "hams_test", env_file=env_path)

        mock_apply_permissions.assert_called_once_with(env_path, "odoo:odoo", 0o600)
        with open(env_path) as f:
            content = f.read()
        self.assertIn("DB_NAME=hams_test\n", content)
        self.assertIn("DB_USER=cache_manager_ro\n", content)
        self.assertRegex(content, r"DB_PASS=\S{20,}\n")
        mode = os.stat(env_path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_rotated_password_differs_from_whatever_a_prior_call_wrote(self):
        run_cmd = MagicMock()
        self.safe_patch_object(infra, "_role_exists", return_value=False)
        self.safe_patch_object(infra, "apply_permissions")
        env_path = self._env_path()

        infra._provision_cache_manager_role(run_cmd, "hams_test", env_file=env_path)
        with open(env_path) as f:
            first_pass = [l for l in f if l.startswith("DB_PASS=")][0]

        self.safe_patch_object(infra, "_role_exists", return_value=True)
        infra._provision_cache_manager_role(run_cmd, "hams_test", env_file=env_path)
        with open(env_path) as f:
            second_pass = [l for l in f if l.startswith("DB_PASS=")][0]

        self.assertNotEqual(first_pass, second_pass)


class RoleExistsTests(_SafePatchTestCase):
    def test_never_splices_role_name_into_a_shell_or_sql_string(self):
        malicious_name = "x'; DROP TABLE pg_roles; --"
        mock_run = self.safe_patch_object(
            infra.subprocess, "run",
            return_value=MagicMock(returncode=0, stdout="1\n"),
        )
        result = infra._role_exists(malicious_name)
        self.assertTrue(result)

        cmd = mock_run.call_args[0][0]
        kwargs = mock_run.call_args.kwargs
        self.assertNotIn("bash", cmd, f"expected no shell invocation: {cmd!r}")
        # Real bug found and fixed 2026-09-13 hardware-qualifying pi500-1: see
        # CreateOdooRoleIfMissingTests.test_role_creation_sql_is_not_passed_via_dash_c
        # for the full investigation -- `:'variable'` substitution never applies via
        # `-c`/`-tAc`, only over stdin.
        self.assertNotIn("-tAc", cmd)
        sql_text = kwargs["input"]
        self.assertNotIn(malicious_name, sql_text)
        self.assertIn(":'role_name'", sql_text)

    def test_returns_false_when_the_role_is_absent(self):
        self.safe_patch_object(
            infra.subprocess, "run",
            return_value=MagicMock(returncode=0, stdout=""),
        )
        self.assertFalse(infra._role_exists("odoo"))


class DatabaseExistsAndOwnerTests(_SafePatchTestCase):
    def test_database_exists_never_splices_db_name_into_a_shell_or_sql_string(self):
        malicious_name = "x'; DROP DATABASE hams_prod; --"
        mock_run = self.safe_patch_object(
            infra.subprocess, "run",
            return_value=MagicMock(returncode=0, stdout="1\n"),
        )
        result = infra._database_exists(malicious_name)
        self.assertTrue(result)

        cmd = mock_run.call_args[0][0]
        kwargs = mock_run.call_args.kwargs
        # No shell is invoked at all (no "bash" in the argv), and the SQL
        # text itself never contains the raw malicious value.
        self.assertNotIn("bash", cmd)
        # Real bug found and fixed 2026-09-13 hardware-qualifying pi500-1: psql's
        # `:'variable'` substitution does not apply at all when SQL is passed via
        # `-c`/`-tAc` -- confirmed directly against a real Postgres 18 instance on
        # both a real Raspberry Pi 500 and the x86_64 dev box -- only when it's
        # read as a script over stdin. The SQL now goes over stdin (`input=`), and
        # `-tAc` is gone from the argv entirely (replaced by the boolean `-tA`).
        self.assertNotIn("-tAc", cmd)
        self.assertIn("-tA", cmd)
        sql_text = kwargs["input"]
        self.assertNotIn(malicious_name, sql_text)
        self.assertIn(":'db_name'", sql_text)

    def test_create_database_if_missing_only_creates_when_absent(self):
        run_cmd = MagicMock()
        self.safe_patch_object(infra, "_database_exists", return_value=True)
        infra._create_database_if_missing(run_cmd, "hams_test")
        run_cmd.assert_not_called()

        self.safe_patch_object(infra, "_database_exists", return_value=False)
        infra._create_database_if_missing(run_cmd, "hams_test")
        run_cmd.assert_called_once_with(["sudo", "-u", "postgres", "createdb", "-O", "odoo", "hams_test"])

    def test_alter_database_owner_never_splices_db_name_into_a_shell_or_sql_string(self):
        malicious_name = 'x"; DROP DATABASE hams_prod; --'
        run_cmd = MagicMock()
        infra._alter_database_owner_to_odoo(run_cmd, malicious_name)

        cmd = run_cmd.call_args[0][0]
        kwargs = run_cmd.call_args.kwargs
        self.assertNotIn("bash", cmd)
        # Same real `-c` bug as _database_exists above, fixed the same way: the
        # SQL is piped over stdin, not passed via -c.
        self.assertNotIn("-c", cmd)
        sql_text = kwargs["input"]
        self.assertNotIn(malicious_name, sql_text)
        self.assertIn(':"db_name"', sql_text)
        self.assertEqual(cmd[cmd.index("-v") + 1], f"db_name={malicious_name}")


class GetInstalledModuleNamesTests(_SafePatchTestCase):
    def test_returns_empty_set_when_the_database_does_not_exist(self):
        self.safe_patch_object(infra, "_database_exists", return_value=False)
        mock_run = self.safe_patch_object(infra.subprocess, "run")
        self.assertEqual(infra._get_installed_module_names("hams_test"), set())
        mock_run.assert_not_called()

    def test_parses_one_module_name_per_line_from_a_real_query_result(self):
        self.safe_patch_object(infra, "_database_exists", return_value=True)
        self.safe_patch_object(
            infra.subprocess, "run",
            return_value=MagicMock(returncode=0, stdout="ham_base\nham_logbook\n\n", stderr=""),
        )
        result = infra._get_installed_module_names("hams_prod")
        self.assertEqual(result, {"ham_base", "ham_logbook"})

    def test_a_fresh_database_with_no_ir_module_module_table_yet_returns_empty_set(self):
        # A genuinely fresh database (created but never initialized) has no
        # ir_module_module table at all -- the query itself fails with
        # "relation ... does not exist", which must be read as "nothing
        # installed yet" (matching this function's pre-existing behavior
        # for a fresh database), not surfaced as a warning/error.
        self.safe_patch_object(infra, "_database_exists", return_value=True)
        self.safe_patch_object(
            infra.subprocess, "run",
            return_value=MagicMock(
                returncode=1, stdout="",
                stderr='ERROR:  relation "ir_module_module" does not exist',
            ),
        )
        mock_warning = self.safe_patch_object(infra._logger, "warning")
        result = infra._get_installed_module_names("hams_test")
        self.assertEqual(result, set())
        mock_warning.assert_not_called()

    def test_an_unexpected_query_failure_stops_instead_of_reporting_nothing_installed(self):
        # An empty set would send every module to `-i` (a no-op for an installed one), so a provisioning run would
        # succeed while applying none of its updates.
        self.safe_patch_object(infra, "_database_exists", return_value=True)
        self.safe_patch_object(
            infra.subprocess, "run",
            return_value=MagicMock(returncode=1, stdout="", stderr="FATAL: connection refused"),
        )
        with self.assertRaises(RuntimeError) as ctx:
            infra._get_installed_module_names("hams_prod")
        self.assertIn("connection refused", str(ctx.exception))
        self.assertIn("hams_prod", str(ctx.exception))


class SplitModulesByInstallStateTests(unittest.TestCase):
    def test_a_module_not_yet_installed_goes_to_install(self):
        to_install, to_update = infra._split_modules_by_install_state(
            {"ham_base"}, installed_modules=set()
        )
        self.assertEqual(to_install, ["ham_base"])
        self.assertEqual(to_update, [])

    def test_an_already_installed_module_goes_to_update_not_install(self):
        # This is the real bug the split exists to fix: a module already
        # 'installed' passed to `-i` again is a no-op in Odoo, so it must
        # route to `-u` instead to actually pick up a later change.
        to_install, to_update = infra._split_modules_by_install_state(
            {"ham_communications_consent"},
            installed_modules={"ham_communications_consent"},
        )
        self.assertEqual(to_install, [])
        self.assertEqual(to_update, ["ham_communications_consent"])

    def test_a_mix_of_new_and_already_installed_modules_splits_correctly(self):
        to_install, to_update = infra._split_modules_by_install_state(
            {"ham_base", "ham_communications_consent", "ham_new_module"},
            installed_modules={"ham_base", "ham_communications_consent"},
        )
        self.assertEqual(to_install, ["ham_new_module"])
        self.assertEqual(to_update, ["ham_base", "ham_communications_consent"])


class DiscoverHamsComDirTests(_TmpDirTestCase):
    """Regression test for a real production incident, 2026-09-23: fixing
    provision.py's own repo_root computation (a separate, earlier fix)
    exposed this function's own ambiguity, since hams_open has its own,
    unrelated top-level daemons/ directory -- the exact real
    /opt/hams/src/{hams_com,hams_open} sibling layout is reproduced here
    directly, not simplified away, since that's precisely what the bug
    depended on."""

    def _make_hams_open(self, with_daemons=True):
        hams_open = os.path.join(self.tmp, "hams_open")
        if with_daemons:
            os.makedirs(os.path.join(hams_open, "daemons"))
        else:
            os.makedirs(hams_open)
        return hams_open

    def _make_sibling_hams_com(self):
        hams_com = os.path.join(self.tmp, "hams_com")
        os.makedirs(os.path.join(hams_com, "ham_base"))
        with open(os.path.join(hams_com, "ham_base", "__manifest__.py"), "w") as f:
            f.write("{}")
        return hams_com

    def test_finds_the_real_sibling_hams_com_even_though_hams_open_has_its_own_daemons_dir(self):
        hams_open = self._make_hams_open(with_daemons=True)
        hams_com = self._make_sibling_hams_com()
        self.assertEqual(infra._discover_hams_com_dir(hams_open), os.path.abspath(hams_com))

    def test_returns_repo_root_itself_when_it_really_is_hams_com(self):
        # The /app-style single-repo layout: repo_root IS hams_com.
        repo_root = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(repo_root, "ham_base"))
        with open(os.path.join(repo_root, "ham_base", "__manifest__.py"), "w") as f:
            f.write("{}")
        self.assertEqual(infra._discover_hams_com_dir(repo_root), os.path.abspath(repo_root))

    def test_returns_none_when_hams_com_cannot_be_found_anywhere(self):
        hams_open = self._make_hams_open(with_daemons=True)
        self.assertIsNone(infra._discover_hams_com_dir(hams_open))


class RefuseUnsafeTestDbDropTests(unittest.TestCase):
    def test_refuses_to_drop_the_well_known_prod_db_name(self):
        # Regression test for a real destructive-operation bug:
        # load_and_prompt_env() reads *.env files unconditionally (before
        # the is_test branch even runs) and populates DB_NAME via
        # setdefault, so a stale/shared /opt/hams/etc/db.env from a prior
        # non-test provisioning run silently survives into a later
        # is_test=True run. Without this guard, provision_environment's
        # test-mode branch would run `dropdb --if-exists hams_prod`.
        with self.assertRaisesRegex(RuntimeError, "hams_prod"):
            infra._refuse_if_unsafe_test_db_drop("hams_prod")

    def test_does_not_raise_for_an_ordinary_test_db_name(self):
        infra._refuse_if_unsafe_test_db_drop("hams_test")  # must not raise
        infra._refuse_if_unsafe_test_db_drop("some_custom_test_db")  # must not raise


class InitializeOdooDatabaseInjectionTests(_SafePatchTestCase):
    def setUp(self):
        # _get_installed_module_names() queries the real database via a
        # real subprocess.run() (psql) -- default it to "nothing installed"
        # (the -i/-u split's original, pre-split behavior: everything goes
        # to -i) for every test in this class so none of them accidentally
        # shells out for real. Tests below that specifically exercise the
        # split override this per-test.
        self.safe_patch_object(infra, "_get_installed_module_names", return_value=set())

    def _run_with_dirs(self, hams_open_dir, hams_com_dir, run_cmd):
        self.safe_patch_object(infra.os, "listdir", return_value=["ham_base"])
        # Force at least one "module" so the function doesn't bail out
        # early on "no custom modules found" -- both the repo dirs
        # themselves and every module dir's own __manifest__.py must
        # appear to exist.
        self.safe_patch_object(
            infra.os.path, "exists",
            side_effect=lambda p: p.endswith("__manifest__.py") or p in (hams_com_dir, hams_open_dir),
        )
        self.safe_patch_object(infra.os.path, "isdir", return_value=True)
        infra.initialize_odoo_database(run_cmd, hams_open_dir, hams_com_dir)

    def test_a_single_quote_in_a_repo_dir_does_not_break_out_of_the_shell_quoting(self):
        # Regression test for a real shell-injection bug: the prior code
        # f-string-interpolated addons_path_str (built from hams_com_dir/
        # hams_open_dir) directly into `bash -c "echo '...' >> ..."`; a
        # single quote in either directory path broke out of the quoted
        # echo argument and let arbitrary shell commands run as root via
        # sudo.
        malicious_dir = "/tmp/repo'; touch /tmp/PWNED; echo '"
        run_cmd = MagicMock()
        self._run_with_dirs(malicious_dir, "/tmp/hams_com", run_cmd)

        addons_path_calls = [
            c.args[0] for c in run_cmd.call_args_list
            if len(c.args[0]) >= 3 and c.args[0][:2] == ["sudo", "bash"]
            and "addons_path" in c.args[0][3]
        ]
        self.assertTrue(addons_path_calls, "expected an `addons_path` `sudo bash -c ...` call")
        script = addons_path_calls[0][3]
        # shlex.split must parse the echo's argument as a single, complete
        # token containing the whole malicious path literally -- if the
        # quoting were broken, shlex would either raise or split it into
        # multiple tokens (the injected `touch` becoming its own command
        # instead of inert text inside the echo).
        parsed = shlex.split(script)
        self.assertIn("echo", parsed)
        echoed = parsed[parsed.index("echo") + 1]
        self.assertIn(malicious_dir, echoed)
        self.assertNotIn("touch", [t for t in parsed if t != echoed])

    def test_a_failure_stopping_odoo_service_is_logged_and_does_not_abort_init(self):
        # Regression test for the `# audit-ignore-catch-all` tag added to this
        # handler (it already logged via _logger.warning, just lacked the
        # tag check_burn_list.py's catch-all-exception rule requires):
        # stopping odoo.service before init is explicitly best-effort (a
        # fresh box may not have the service installed/running yet), so a
        # failure here must be logged, not raised, and initialization must
        # still proceed to the actual `odoo -i ...` call below it.
        def run_cmd(cmd, *a, **kw):
            if cmd[:3] == ["sudo", "systemctl", "stop"]:
                raise RuntimeError("simulated: odoo.service not found")
            return MagicMock()

        run_cmd_mock = MagicMock(side_effect=run_cmd)
        with self.assertLogs("infrastructure", level="WARNING") as log_ctx:
            self._run_with_dirs("/tmp/hams_open", "/tmp/hams_com", run_cmd_mock)
        self.assertTrue(
            any("Failed to stop odoo.service before init" in msg for msg in log_ctx.output)
        )
        init_calls = [
            c.args[0] for c in run_cmd_mock.call_args_list
            if len(c.args[0]) >= 2 and c.args[0][0] == "sudo" and c.args[0][1] == "-u"
        ]
        self.assertTrue(init_calls, "expected the actual `odoo -i ...` init call to still run")

    def test_database_name_is_used_for_the_module_install_not_a_hardcoded_one(self):
        # Regression test: the install used "-d hams_test" even on a production run, whose
        # empty, already-provisioned database is DB_NAME (default hams_prod).
        run_cmd = MagicMock()
        self.safe_patch_object(infra.os, "listdir", return_value=["ham_base"])
        self.safe_patch_object(
            infra.os.path, "exists",
            side_effect=lambda p: p.endswith("__manifest__.py") or p in ("/a", "/b"),
        )
        self.safe_patch_object(infra.os.path, "isdir", return_value=True)
        infra.initialize_odoo_database(run_cmd, "/a", "/b", db_name="hams_prod")
        install = [c.args[0] for c in run_cmd.call_args_list if "--stop-after-init" in c.args[0]][0]
        self.assertEqual(install[install.index("-d") + 1], "hams_prod")

    def test_database_name_defaults_to_the_test_database(self):
        run_cmd = MagicMock()
        self.safe_patch_object(infra.os, "listdir", return_value=["ham_base"])
        self.safe_patch_object(
            infra.os.path, "exists",
            side_effect=lambda p: p.endswith("__manifest__.py") or p in ("/a", "/b"),
        )
        self.safe_patch_object(infra.os.path, "isdir", return_value=True)
        infra.initialize_odoo_database(run_cmd, "/a", "/b")
        install = [c.args[0] for c in run_cmd.call_args_list if "--stop-after-init" in c.args[0]][0]
        self.assertEqual(install[install.index("-d") + 1], "hams_test")

    def test_an_already_installed_custom_module_gets_dash_u_not_a_no_op_dash_i(self):
        # The real bug this whole split exists to fix, exercised end to end
        # through initialize_odoo_database itself (not just the pure split
        # helper above): a module the target database already has installed
        # -- exactly hams_prod's own situation for ham_communications_consent
        # after Phase 1 -- must reach the actual `odoo` command via `-u`, or a
        # later change to that module (new fields, new models) would never
        # apply to a real, already-provisioned database.
        run_cmd = MagicMock()
        self.safe_patch_object(
            infra, "_get_installed_module_names",
            return_value={"ham_communications_consent"},
        )
        self.safe_patch_object(
            infra.os, "listdir",
            return_value=["ham_base", "ham_communications_consent"],
        )
        self.safe_patch_object(
            infra.os.path, "exists",
            side_effect=lambda p: p.endswith("__manifest__.py") or p in ("/a", "/b"),
        )
        self.safe_patch_object(infra.os.path, "isdir", return_value=True)
        infra.initialize_odoo_database(run_cmd, "/a", "/b", db_name="hams_prod")

        install = [c.args[0] for c in run_cmd.call_args_list if "--stop-after-init" in c.args[0]][0]
        install_value = install[install.index("-i") + 1]
        # base and the never-before-installed ham_base go to -i; the
        # already-installed ham_communications_consent must NOT be in there.
        self.assertIn("base", install_value.split(","))
        self.assertIn("ham_base", install_value.split(","))
        self.assertNotIn("ham_communications_consent", install_value.split(","))
        # "-u" also appears earlier as part of "sudo -u odoo"; the module-update
        # flag is the one after "-d", so search from there.
        after_d = install.index("-d")
        self.assertIn("-u", install[after_d:])
        update_idx = after_d + install[after_d:].index("-u")
        self.assertEqual(install[update_idx + 1], "ham_communications_consent")

    def test_gevent_memory_limits_are_written_to_odoo_conf(self):
        # Tests [@ANCHOR: infrastructure:odoo_conf_gevent_memory_limits]
        # Regression for the 2026-09-30 gevent restart loop: an unset
        # limit_memory_soft_gevent fell back to a 2 GiB ceiling the worker's
        # own VSZ crossed under ordinary websocket load. Provisioning must
        # write a soft limit with headroom AND a gevent hard limit above it
        # (Odoo's limit_memory_hard default, 2.5 GiB, is below 3 GiB).
        run_cmd = MagicMock()
        self._run_with_dirs("/tmp/hams_open", "/tmp/hams_com", run_cmd)
        cmds = [c.args[0] for c in run_cmd.call_args_list]
        limits = dict(infra.ODOO_CONF_GEVENT_MEMORY_LIMITS)
        self.assertEqual(limits["limit_memory_soft_gevent"], 3 * 1024 ** 3)
        self.assertEqual(limits["limit_memory_hard_gevent"], 4 * 1024 ** 3)
        self.assertLess(limits["limit_memory_soft_gevent"], limits["limit_memory_hard_gevent"])
        for key, value in limits.items():
            delete = ["sudo", "sed", "-i", f"/^{key}[[:space:]]*=/d", "/etc/odoo/odoo.conf"]
            append = [
                c for c in cmds
                if c[:2] == ["sudo", "bash"] and shlex.split(c[3])[:2] == ["echo", f"{key} = {value}"]
            ]
            self.assertIn(delete, cmds)
            self.assertEqual(len(append), 1, f"expected exactly one append of {key}")
            # Delete before append, or a re-provision would remove the fresh line.
            self.assertLess(cmds.index(delete), cmds.index(append[0]))
        # The actual sed address used must match the key's own line but not
        # a longer key sharing its prefix, and no sed in this function may
        # delete the operator-managed limit_memory_soft/limit_memory_hard.
        conf_lines = [
            "limit_memory_soft = 1", "limit_memory_hard = 4294967296",
            "limit_memory_soft_gevent = 1", "limit_memory_hard_gevent = 1",
            "limit_memory_soft_gevent_x = 1",
        ]
        for c in cmds:
            if c[:3] != ["sudo", "sed", "-i"] or "limit_memory" not in c[3]:
                continue
            address = c[3][1:-2].replace("[[:space:]]", r"\s")  # strip "/" ... "/d"
            matched = [line for line in conf_lines if re.match(address, line)]
            self.assertEqual(len(matched), 1, f"{c[3]} must delete exactly one key, got {matched}")
            self.assertIn(matched[0].split(" = ")[0], limits)


class RustToolchainPackageTests(unittest.TestCase):
    def test_cargo_is_installed_by_provisioning_because_the_rust_daemon_hook_needs_it(self):
        names = [
            p.get("debian_name", p["name"])
            for p in infra.MANIFEST["apt_packages"]
            if "early_prod" in p["environments"]
        ]
        # Debian's own plain cargo (1.85) is too old for the daemons' dependencies; cargo-web is 1.96.
        self.assertIn("cargo-web", names)


class HoldOdooTests(unittest.TestCase):
    """provision_environment() is too host-dependent to execute here, so these read its source,
    like the neighbouring tests. The behaviour being pinned: with hold_odoo the database module
    install and the service smoketest (which starts Odoo and every daemon) are both skipped."""

    def test_hold_odoo_parameter_exists_and_defaults_to_false(self):
        param = inspect.signature(infra.provision_environment).parameters["hold_odoo"]
        self.assertIs(param.default, False)

    def test_hold_branch_comes_before_and_excludes_the_init_and_smoketest_calls(self):
        source = inspect.getsource(infra.provision_environment)
        hold_idx = source.index("if hold_odoo:")
        elif_idx = source.index("elif not is_isolated_ns:", hold_idx)
        init_idx = source.index("initialize_odoo_database(\n", hold_idx)
        smoke_idx = source.index("run_post_provision_smoketest(has_hams_com", hold_idx)
        # The init and smoketest calls sit inside the elif of the hold check, never
        # unconditionally after it.
        self.assertLess(hold_idx, elif_idx)
        self.assertLess(elif_idx, init_idx)
        self.assertLess(init_idx, smoke_idx)

    def test_the_database_install_receives_db_name_from_env_vars(self):
        source = inspect.getsource(infra.provision_environment)
        self.assertIn('db_name=env_vars.get("DB_NAME", "hams_test")', source)


class LinkedSystemdTimersAreActuallyEnabledTests(unittest.TestCase):
    """provision_environment() is too host-dependent to execute here (see this file's own
    established pattern for it), so this reads its source, like the neighbouring tests.

    Real bug found live on hams1, 2026-09-23: linking a unit into /etc/systemd/system with a
    bare os.symlink() leaves it in systemd's own "linked" state, not "enabled" -- [Install]
    WantedBy=timers.target is only acted on by an explicit `systemctl enable`, which this
    provisioning step never issued. Confirmed live: ~18 of ~20 hams.com-specific timers sat
    "linked (dead)" with zero timer-driven run history since the box was first provisioned,
    silently never syncing regulator callbook data, AMSAT TLE, contest calendars, POTA/SOTA, or
    ingesting inbound support email -- caught only because a night-watch session happened to
    check `systemctl list-timers --all` days later."""

    def test_every_linked_timer_is_enabled_after_the_linking_loop(self):
        source = inspect.getsource(infra.provision_environment)
        link_idx = source.index('_logger.info("[*] Linking custom systemd units...")')
        enable_idx = source.index('"systemctl", "enable", unit', link_idx)
        self.assertGreater(
            enable_idx,
            link_idx,
            "the systemctl enable call must come after the symlinking loop, "
            "not before it (nothing to enable yet before units are linked)",
        )
        self.assertIn('item.endswith((".timer", ".path"))', source[link_idx:enable_idx])

    def test_enable_is_not_enable_now_to_avoid_an_immediate_first_run_stampede(self):
        # `enable --now` would force every one of ~20 daemons to run for the very first time
        # the instant this provisioning step runs -- including sensitive ones like inbound
        # support-email ingestion, which could process a real backlog and fire real
        # notification mail. Plain `enable` only wires the timer into
        # timers.target.wants/ so it fires at its own next OnCalendar tick.
        source = inspect.getsource(infra.provision_environment)
        self.assertIn('["systemctl", "enable", unit]', source)
        self.assertNotIn('["systemctl", "enable", "--now"', source)

    def test_daemon_reload_runs_before_any_enable_call(self):
        source = inspect.getsource(infra.provision_environment)
        reload_idx = source.index('["systemctl", "daemon-reload"]')
        enable_idx = source.index('"systemctl", "enable", unit')
        self.assertLess(reload_idx, enable_idx)

    def test_path_units_are_linked_and_enabled_the_same_way_as_timers(self):
        # Added 2026-10-01 alongside this codebase's first .path unit
        # (hams-pgbackrest-backup.path, ADR 0103): a .path unit is an
        # [Install] WantedBy= activation unit exactly like a .timer, with
        # the identical "linked but not enabled" footgun this whole test
        # class exists to guard against -- it must get the same explicit
        # `systemctl enable` treatment, not just the symlink.
        source = inspect.getsource(infra.provision_environment)
        link_idx = source.index('_logger.info("[*] Linking custom systemd units...")')
        self.assertIn('item.endswith((".service", ".timer", ".path"))', source[link_idx:])
        self.assertIn(
            'linked_activation_units.append(item)',
            source[link_idx:],
        )


class LoadAndPromptEnvRabbitmqUserDefaultTests(unittest.TestCase):
    def test_rmq_user_defaults_to_a_real_non_guest_service_account(self):
        # Real bug found and fixed 2026-09-13 hardware-qualifying pi500-1: this used
        # to default to the literal "guest" (RabbitMQ's factory-default account),
        # which daemons/adif_processor/main.py's own _require_rabbitmq_credentials
        # deliberately refuses for either RMQ_USER or RMQ_PASS -- see
        # _create_rabbitmq_user_if_missing's own docstring for the full
        # investigation. Never caught on the dev box because adif.processor.service
        # had simply never been started there either.
        env_vars = {"DOMAIN": "hams.com"}
        infra.load_and_prompt_env(env_vars, is_test=False)
        self.assertNotEqual(env_vars["RMQ_USER"], "guest")
        self.assertTrue(env_vars["RMQ_USER"], "RMQ_USER must not be empty")


class RabbitmqUserExistsTests(_SafePatchTestCase):
    def test_returns_true_when_the_user_is_present(self):
        self.safe_patch_object(
            infra.subprocess, "run",
            return_value=MagicMock(
                returncode=0,
                stdout='[\n{"user":"guest","tags":["administrator"]},\n{"user":"hams_rabbitmq","tags":[]}\n]',
            ),
        )
        self.assertTrue(infra._rabbitmq_user_exists("hams_rabbitmq"))

    def test_returns_false_when_the_user_is_absent(self):
        self.safe_patch_object(
            infra.subprocess, "run",
            return_value=MagicMock(returncode=0, stdout='[\n{"user":"guest","tags":["administrator"]}\n]'),
        )
        self.assertFalse(infra._rabbitmq_user_exists("hams_rabbitmq"))

    def test_returns_false_on_a_non_zero_exit_or_malformed_json(self):
        self.safe_patch_object(
            infra.subprocess, "run",
            return_value=MagicMock(returncode=1, stdout=""),
        )
        self.assertFalse(infra._rabbitmq_user_exists("hams_rabbitmq"))

        self.safe_patch_object(
            infra.subprocess, "run",
            return_value=MagicMock(returncode=0, stdout="not json"),
        )
        self.assertFalse(infra._rabbitmq_user_exists("hams_rabbitmq"))


class CreateRabbitmqUserIfMissingTests(_SafePatchTestCase):
    def test_refuses_to_provision_a_user_literally_named_guest(self):
        # Real bug this whole function exists to close: provisioning "guest" as
        # this project's own real service account would defeat the entire point
        # -- adif_processor's own security check specifically rejects it.
        with self.assertRaises(RuntimeError):
            infra._create_rabbitmq_user_if_missing(MagicMock(), "guest", "somepass")

    def test_adds_a_new_user_and_grants_permissions_when_absent(self):
        self.safe_patch_object(infra, "_rabbitmq_user_exists", return_value=False)
        run_cmd = MagicMock()
        infra._create_rabbitmq_user_if_missing(run_cmd, "hams_rabbitmq", "realpass123")

        calls = [c.args[0] for c in run_cmd.call_args_list]
        self.assertIn(["rabbitmqctl", "add_user", "hams_rabbitmq", "realpass123"], calls)
        self.assertNotIn(["rabbitmqctl", "change_password", "hams_rabbitmq", "realpass123"], calls)
        self.assertIn(
            ["rabbitmqctl", "set_permissions", "-p", "/", "hams_rabbitmq", ".*", ".*", ".*"],
            calls,
        )

    def test_changes_the_password_and_re_grants_permissions_when_already_present(self):
        self.safe_patch_object(infra, "_rabbitmq_user_exists", return_value=True)
        run_cmd = MagicMock()
        infra._create_rabbitmq_user_if_missing(run_cmd, "hams_rabbitmq", "realpass123")

        calls = [c.args[0] for c in run_cmd.call_args_list]
        self.assertIn(["rabbitmqctl", "change_password", "hams_rabbitmq", "realpass123"], calls)
        self.assertNotIn(["rabbitmqctl", "add_user", "hams_rabbitmq", "realpass123"], calls)
        self.assertIn(
            ["rabbitmqctl", "set_permissions", "-p", "/", "hams_rabbitmq", ".*", ".*", ".*"],
            calls,
        )


class RabbitmqGuestDefaultRemovedTests(unittest.TestCase):
    """provision_environment() applies MANIFEST["env_defaults"] with setdefault() BEFORE
    load_and_prompt_env(); a guest/guest entry there pre-empted the real production defaults."""

    def test_env_defaults_carry_no_rabbitmq_credentials(self):
        self.assertNotIn("RMQ_USER", infra.MANIFEST["env_defaults"])
        self.assertNotIn("RMQ_PASS", infra.MANIFEST["env_defaults"])

    def test_production_env_after_env_defaults_gets_the_real_account_and_a_generated_password(self):
        # The exact order provision_environment() uses.
        env_vars = {"DOMAIN": "hams.com", "CLOUDFLARE_ZONE_ID": "none"}
        for k, v in infra.MANIFEST["env_defaults"].items():
            env_vars.setdefault(k, v)
        infra.load_and_prompt_env(env_vars, is_test=False)
        self.assertEqual(env_vars["RMQ_USER"], "hams_rabbitmq")
        self.assertNotEqual(env_vars["RMQ_PASS"], "guest")
        self.assertGreaterEqual(len(env_vars["RMQ_PASS"]), 32)

    def test_test_env_keeps_the_private_brokers_factory_account(self):
        env_vars = {}
        for k, v in infra.MANIFEST["env_defaults"].items():
            env_vars.setdefault(k, v)
        infra.load_and_prompt_env(env_vars, is_test=True)
        self.assertEqual((env_vars["RMQ_USER"], env_vars["RMQ_PASS"]), ("guest", "guest"))


class DeleteRabbitmqGuestUserTests(_SafePatchTestCase):
    def test_deletes_guest_when_present(self):
        # Tests [@ANCHOR: infrastructure:_delete_rabbitmq_guest_user_if_present]
        self.safe_patch_object(infra, "_rabbitmq_user_exists", side_effect=lambda user: user == "guest")
        run_cmd = MagicMock()
        infra._delete_rabbitmq_guest_user_if_present(run_cmd)
        run_cmd.assert_called_once_with(["rabbitmqctl", "delete_user", "guest"])

    def test_does_nothing_when_guest_is_already_gone(self):
        self.safe_patch_object(infra, "_rabbitmq_user_exists", return_value=False)
        run_cmd = MagicMock()
        infra._delete_rabbitmq_guest_user_if_present(run_cmd)
        run_cmd.assert_not_called()

    def test_provisioning_deletes_guest_only_for_production_and_only_after_the_real_account(self):
        source = inspect.getsource(infra.provision_environment)
        create_idx = source.index("_create_rabbitmq_user_if_missing(run_cmd_func, rmq_user, rmq_pass)")
        guard_idx = source.index("if not is_test:", create_idx)
        delete_idx = source.index("_delete_rabbitmq_guest_user_if_present(run_cmd_func)")
        self.assertLess(create_idx, guard_idx)
        self.assertLess(guard_idx, delete_idx)


class EnsureLineInFileTests(_SafePatchTestCase):
    def test_appends_once_and_is_idempotent(self):
        # Tests [@ANCHOR: infrastructure:_ensure_line_in_file]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "rabbitmq-env.conf")
            with open(path, "w") as f:
                f.write("# comment\n#NODE_IP_ADDRESS=127.0.0.1")  # no trailing newline
            self.assertTrue(infra._ensure_line_in_file(path, "NODE_IP_ADDRESS=127.0.0.1"))
            self.assertFalse(infra._ensure_line_in_file(path, "NODE_IP_ADDRESS=127.0.0.1"))
            with open(path) as f:
                lines = f.read().splitlines()
            self.assertEqual(lines, ["# comment", "#NODE_IP_ADDRESS=127.0.0.1", "NODE_IP_ADDRESS=127.0.0.1"])

    def test_creates_a_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "new.conf")
            self.assertTrue(infra._ensure_line_in_file(path, "include /etc/redis/hams-acl.conf"))
            with open(path) as f:
                self.assertEqual(f.read(), "include /etc/redis/hams-acl.conf\n")

    def test_rabbitmq_env_conf_is_no_longer_appended_blindly(self):
        source = inspect.getsource(infra.provision_environment)
        self.assertIn('_ensure_line_in_file("/etc/rabbitmq/rabbitmq-env.conf", "NODE_IP_ADDRESS=127.0.0.1")', source)
        self.assertNotIn('open("/etc/rabbitmq/rabbitmq-env.conf", "a")', source)


class RedisAclTests(_SafePatchTestCase):
    def test_production_env_gets_redis_credentials_and_a_matching_url(self):
        # Tests [@ANCHOR: infrastructure:redis_url]
        env_vars = {"DOMAIN": "hams.com", "CLOUDFLARE_ZONE_ID": "none"}
        infra.load_and_prompt_env(env_vars, is_test=False)
        self.assertEqual(env_vars["REDIS_USERNAME"], "hams_redis")
        self.assertGreaterEqual(len(env_vars["REDIS_PASSWORD"]), 32)
        self.assertEqual(
            env_vars["REDIS_URL"],
            f"redis://hams_redis:{env_vars['REDIS_PASSWORD']}@redis:6379/0",
        )

    def test_an_existing_password_is_kept_and_the_url_follows_it(self):
        env_vars = {
            "DOMAIN": "hams.com", "CLOUDFLARE_ZONE_ID": "none",
            "REDIS_PASSWORD": "p@ss/word", "REDIS_URL": "redis://stale@redis:6379/0",
        }
        infra.load_and_prompt_env(env_vars, is_test=False)
        self.assertEqual(env_vars["REDIS_PASSWORD"], "p@ss/word")
        self.assertEqual(env_vars["REDIS_URL"], "redis://hams_redis:p%40ss%2Fword@redis:6379/0")

    def test_test_env_gets_no_redis_credentials(self):
        env_vars = {}
        infra.load_and_prompt_env(env_vars, is_test=True)
        self.assertNotIn("REDIS_PASSWORD", env_vars)
        self.assertNotIn("REDIS_USERNAME", env_vars)

    def test_redis_env_file_carries_the_credentials(self):
        self.assertEqual(
            infra.MANIFEST["env_groups"]["redis.env"],
            ["REDIS_HOST", "REDIS_PORT", "REDIS_USERNAME", "REDIS_PASSWORD", "REDIS_URL"],
        )

    def test_acl_fragment_turns_default_off_and_stores_only_a_hash(self):
        # Tests [@ANCHOR: infrastructure:_redis_acl_include_content]
        content = infra._redis_acl_include_content("hams_redis", "secretvalue")
        digest = hashlib.sha256(b"secretvalue").hexdigest()
        lines = [line for line in content.splitlines() if not line.startswith("#")]
        self.assertEqual(
            lines,
            [
                "user default off resetpass resetkeys resetchannels -@all",
                f"user hams_redis on #{digest} ~* &* +@all",
            ],
        )
        self.assertNotIn("secretvalue", content)

    def test_write_include_creates_fragment_and_include_line_idempotently(self):
        # Tests [@ANCHOR: infrastructure:_write_redis_acl_include]
        self.safe_patch_object(infra, "apply_permissions")
        with tempfile.TemporaryDirectory() as directory:
            conf = os.path.join(directory, "redis.conf")
            include = os.path.join(directory, "hams-acl.conf")
            with open(conf, "w") as f:
                f.write("bind 127.0.0.1 -::1\n")
            env_vars = {"REDIS_USERNAME": "hams_redis", "REDIS_PASSWORD": "pw"}
            infra._write_redis_acl_include(env_vars, conf, include)
            infra._write_redis_acl_include(env_vars, conf, include)
            with open(conf) as f:
                self.assertEqual(f.read().splitlines(), ["bind 127.0.0.1 -::1", f"include {include}"])
            with open(include) as f:
                self.assertEqual(f.read(), infra._redis_acl_include_content("hams_redis", "pw"))
            self.assertEqual(os.stat(include).st_mode & 0o777, 0o640)
        infra.apply_permissions.assert_called_with(include, "redis:redis", 0o640)

    def test_write_include_refuses_missing_or_default_credentials(self):
        for env_vars in ({}, {"REDIS_USERNAME": "hams_redis"}, {"REDIS_USERNAME": "default", "REDIS_PASSWORD": "pw"}):
            with self.subTest(env_vars=env_vars), self.assertRaises(RuntimeError):
                infra._write_redis_acl_include(env_vars, "/nonexistent/redis.conf", "/nonexistent/acl.conf")

    def test_provisioning_applies_the_acl_for_production_only(self):
        source = inspect.getsource(infra.provision_environment)
        call_idx = source.index("_write_redis_acl_include(env_vars)")
        guard = source.rfind("if not is_test:", 0, call_idx)
        self.assertNotEqual(guard, -1)
        self.assertNotIn("is_test", source[guard + len("if not is_test:"):call_idx])


class AptPackagesManifestTests(unittest.TestCase):
    def test_python3_fitz_is_installed_for_ham_onboardings_real_external_dependency(self):
        # Real, previously-masked missing dependency found 2026-09-13 hardware-
        # qualifying pi500-1 (a genuinely fresh Raspberry Pi 500):
        # ham_onboarding/__manifest__.py declares a real external Python
        # dependency on `fitz` (PyMuPDF's import name), but this project's own
        # apt_packages MANIFEST never installed the package that provides it --
        # `odoo -i ham_onboarding` failed on a fresh box with "external
        # dependency is not met: fitz". Never caught on the dev box because it
        # already had python3-pymupdf/python3-fitz installed incidentally from
        # unrelated prior work. This is a narrow regression guard for this one
        # real finding, not a general manifest-vs-apt-packages cross-checker
        # (a real, separate, larger undertaking -- most external_dependencies
        # entries don't share their apt package's name 1:1, e.g. "yaml" vs
        # "python3-yaml", so a general check needs a real name-mapping table,
        # not attempted here).
        fitz_entries = [
            pkg for pkg in infra.MANIFEST["apt_packages"]
            if pkg.get("debian_name") == "python3-fitz"
        ]
        self.assertTrue(
            fitz_entries,
            "expected an apt_packages MANIFEST entry installing python3-fitz "
            "for ham_onboarding's real fitz/PyMuPDF dependency",
        )
        self.assertIn("early_prod", fitz_entries[0]["environments"])

    def test_python3_dotenv_is_installed_for_distributed_redis_caches_external_dependency(self):
        # distributed_redis_cache's manifest declares python-dotenv as an external dependency, so
        # Odoo will not install that module without it. The first real release provision on a
        # fresh production server (2026-09-21) failed here because nothing installed it.
        entries = [
            pkg for pkg in infra.MANIFEST["apt_packages"]
            if pkg.get("debian_name") == "python3-dotenv"
        ]
        self.assertTrue(entries, "expected an apt_packages MANIFEST entry installing python3-dotenv")
        self.assertIn("early_prod", entries[0]["environments"])

    def test_python3_feedparser_is_installed_for_wa7bnm_contest_syncs_real_runtime_dependency(self):
        # wa7bnm_contest_sync.py's `import feedparser` is a module-level,
        # unconditional import in real daemon code, not test code --
        # confirmed live on hams1, 2026-09-22:
        # wa7bnm.contest.sync.service crashed with ModuleNotFoundError on
        # every run, because feedparser had only ever been installed under
        # `if is_test:` further down in provision_environment() (grouped
        # there with other daemons' genuinely test-only dependencies).
        entries = [
            pkg for pkg in infra.MANIFEST["apt_packages"]
            if pkg.get("debian_name") == "python3-feedparser"
        ]
        self.assertTrue(
            entries,
            "expected an apt_packages MANIFEST entry installing python3-feedparser "
            "for wa7bnm_contest_sync.py's real feedparser dependency",
        )
        self.assertIn("early_prod", entries[0]["environments"])

    def test_python3_aiohttp_is_installed_for_adif_ingress_and_gdpr_csv_exports_real_runtime_dependency(self):
        # The same miscategorization as the feedparser test above, found while root-causing
        # night_shift_todo's adif-ingress-intermittent-aiohttp-modulenotfound-crash-loop report
        # (2026-10-02): daemons/adif_ingress/main.py and daemons/gdpr_csv_export/main.py both do
        # a real, unconditional module-level `import aiohttp` / `from aiohttp import web`, but
        # this package had only ever been installed under `if is_test:` further down in
        # provision_environment() (grouped there with other daemons' genuinely test-only
        # dependencies). With no `early_prod` entry, a fresh/re-provisioning run never
        # guaranteed this package was present before adif.ingress.service (Restart=always,
        # WantedBy=multi-user.target) first tried to start -- explaining the observed
        # intermittent ModuleNotFoundError crash loop on boot.
        entries = [
            pkg for pkg in infra.MANIFEST["apt_packages"]
            if pkg.get("debian_name") == "python3-aiohttp"
        ]
        self.assertTrue(
            entries,
            "expected an apt_packages MANIFEST entry installing python3-aiohttp "
            "for adif_ingress/main.py's and gdpr_csv_export/main.py's real aiohttp dependency",
        )
        self.assertIn("early_prod", entries[0]["environments"])

    def test_adif_ingress_waits_for_its_own_aiohttp_dependency_before_execstart(self):
        # Defense in depth alongside the MANIFEST fix above: adif.ingress.service's own
        # ExecStart crashes with ModuleNotFoundError before main() runs anything if aiohttp
        # is ever transiently unavailable for any reason (a regression in the fix above, a
        # future unattended-upgrades operation mid-flight, etc). Unlike this unit's siblings
        # (adif.processor.service, gdpr.csv.export.service), it previously had no
        # ExecStartPre resource check at all. This pins that it now retries the one real
        # dependency that actually crashed in production, bounded, before ExecStart ever runs.
        entry = next(
            item for item in infra.MANIFEST["static_files"]
            if item.get("path") == "/opt/hams/systemd/adif.ingress.service"
        )
        text = infra.format_env(entry["content"], {})
        pre_line = next(
            line for line in text.splitlines() if line.startswith("ExecStartPre=")
        )
        self.assertIn("import aiohttp", pre_line)
        exec_start_index = text.index("\nExecStart=")
        self.assertLess(text.index(pre_line), exec_start_index)

    def test_awscli_is_installed_for_ses_inbound_mail_ingests_real_runtime_dependency(self):
        # Real gap found live on hams1, 2026-09-22: daemons/ses_inbound_mail_ingest/main.py
        # shells out to the `aws` CLI, but nothing in this MANIFEST ever installed it --
        # ses.inbound.mail.ingest.service crashed with an unhandled FileNotFoundError
        # ('aws' not found), before any of the daemon's own logging ran, which is why
        # journalctl showed zero entries for the unit despite StandardError=journal.
        entries = [
            pkg for pkg in infra.MANIFEST["apt_packages"]
            if pkg.get("debian_name") == "awscli"
        ]
        self.assertTrue(
            entries,
            "expected an apt_packages MANIFEST entry installing awscli for "
            "ses_inbound_mail_ingest's real `aws` CLI dependency",
        )
        self.assertIn("early_prod", entries[0]["environments"])


class StaticFilesEntriesHaveContentTests(unittest.TestCase):
    """Regression test for a real production incident, 2026-09-23: three
    MANIFEST["static_files"] entries (/var/lib/powerdns, /var/lib/powerdns/
    callbook, /opt/hams/hamcall) had none of content/src/url -- they were
    directory-only declarations, meant for MANIFEST["directories"], sitting
    in the wrong list. provision_static_files() iterates every entry in
    "static_files" and, finding none of those three keys, falls through to
    its own "write file content" branch and calls os.open() on the path --
    which crashed with IsADirectoryError the moment any of those three
    paths already existed as a real directory (which, in production, they
    always do -- each is created by a package's own postinst or a daemon's
    own setup before this ever runs). The first of the three was found only
    by actually running provision.py against hams1 mid-release; the other
    two were found by this same check, run proactively, before either
    became the next crash. Every entry in "static_files" must declare one
    of content/src/url -- a directory-only entry belongs in "directories"
    instead."""

    def test_every_static_files_entry_declares_content_src_or_url(self):
        missing = [
            entry["path"] for entry in infra.MANIFEST["static_files"]
            if not any(k in entry for k in ("content", "src", "url"))
        ]
        self.assertEqual(
            missing, [],
            f"static_files entries with none of content/src/url (belong in "
            f"'directories' instead): {missing}",
        )


class Pi500FccUlsSyncManifestTests(unittest.TestCase):
    """night_shift_todo/medium/pi500-1-fcc-uls-sync-not-tracked-in-infrastructure-py-6d9a2f83.md:
    pi500-1's real, currently-working fcc.uls.sync.service/.timer used to exist only as
    hand-edited files on that one physical machine, with nothing in version control to
    reconstruct them from if the box were ever reprovisioned. Confirms the real content (pulled
    directly from /etc/systemd/system/ on pi500-1, 2026-09-30) is captured, and that it is
    tagged with its own "pi500-1" environment -- never "prod"/"test" -- so provision_environment()
    (default environment="prod") never touches it unless something explicitly targets pi500-1."""

    def _pi500_entries(self):
        return [
            entry for entry in infra.MANIFEST["static_files"]
            if entry["path"] in (
                "/etc/systemd/system/fcc.uls.sync.service",
                "/etc/systemd/system/fcc.uls.sync.timer",
            )
        ]

    def test_both_the_real_service_and_timer_are_captured(self):
        paths = {entry["path"] for entry in self._pi500_entries()}
        self.assertEqual(
            paths,
            {"/etc/systemd/system/fcc.uls.sync.service", "/etc/systemd/system/fcc.uls.sync.timer"},
            "expected both the real, live fcc.uls.sync.service and .timer as captured in git",
        )

    def test_both_are_tagged_pi500_1_only_not_prod_or_test(self):
        for entry in self._pi500_entries():
            with self.subTest(path=entry["path"]):
                self.assertEqual(
                    entry["environments"], ["pi500-1"],
                    "must be scoped to its own environment, never prod/test -- provisioning a "
                    "real production or test box must never accidentally lay down pi500-1's "
                    "own residential-egress-path unit file",
                )

    def test_service_content_matches_the_real_deployment_shape(self):
        service = next(
            e for e in self._pi500_entries() if e["path"].endswith("fcc.uls.sync.service")
        )
        content = service["content"]
        # The real, currently-working deployment's own distinguishing features -- confirmed
        # live, 2026-09-30, not assumed: the venv-based ExecStart (not system Python), the
        # UMask=0002 fix (night_shift_history.md's own 2026-09-30 entry), and the residential
        # ODOO_URL pointing at the real production site rather than a local address.
        self.assertIn("UMask=0002", content)
        self.assertIn(
            "/opt/hams/daemons/fcc_uls_sync/.venv/bin/python3", content,
            "must use the real venv interpreter, not system /usr/bin/python3 -- that was the "
            "OLD, stale deployment shape this entry replaces",
        )
        self.assertIn('Environment="ODOO_URL=https://hams.com"', content)

    def test_service_names_its_registry_for_remote_self_rotation(self):
        # hams1's Odoo cannot write pi500-1's key file. Without this variable the daemon
        # never rotates its own key, and hams1's 59-day cron would revoke it instead
        # (night_shift_todo/low/fcc-uls-sync-pi500-key-does-not-auto-rotate-3f8c1d92.md).
        service = next(
            e for e in self._pi500_entries() if e["path"].endswith("fcc.uls.sync.service")
        )
        self.assertIn(
            'Environment="ODOO_KEY_SELF_ROTATE_DAEMON=FCC ULS Sync (pi500-1)"',
            service["content"],
        )

    def test_timer_content_matches_the_real_deployment_shape(self):
        timer = next(e for e in self._pi500_entries() if e["path"].endswith("fcc.uls.sync.timer"))
        self.assertIn("OnCalendar=*-*-* 05:00:00", timer["content"])


class ClaudeWrapperCiOauthTokenTests(unittest.TestCase):
    """Tests [@ANCHOR: infrastructure:claude_wrapper_ci_oauth_token]

    The two Claude CLI wrapper scripts were fixed live on hams1 (2026-10-02) to prefer a
    provisioned `~/.claude/ci_oauth_token` as CLAUDE_CODE_OAUTH_TOKEN; the MANIFEST copy had
    drifted and a re-provisioning run would have reverted it. These run the real rendered
    MANIFEST content (what provision_static_files() writes to disk) under bash, with only the
    account's home path redirected to a temp dir so it can run off-box, and a stand-in `claude`
    that reports what it was given."""

    ACCOUNTS = ("hams_ai_agent", "hams_event_agent")

    def _rendered(self, account):
        path = f"/usr/local/sbin/run-hams-{account.removeprefix('hams_').replace('_', '-')}-claude.sh"
        entry = next(e for e in infra.MANIFEST["static_files"] if e["path"] == path)
        self.assertEqual((entry["owner"], entry["mode"], entry["environments"]), ("root:root", "755", ["prod"]))
        return infra.format_env(entry["content"], {})

    def _run(self, account, token_text):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, account)
            os.makedirs(os.path.join(home, ".claude"))
            os.makedirs(os.path.join(home, ".local", "bin"))
            fake = os.path.join(home, ".local", "bin", "claude")
            with open(fake, "w") as f:
                f.write('#!/bin/bash\necho "cwd=$PWD"\necho "home=$HOME"\n'
                        'echo "token=${CLAUDE_CODE_OAUTH_TOKEN-UNSET}"\necho "args=$*"\n')
            os.chmod(fake, 0o755)
            if token_text is not None:
                with open(os.path.join(home, ".claude", "ci_oauth_token"), "w") as f:
                    f.write(token_text)
            script = self._rendered(account).replace(f"/home/{account}", home)
            env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
            out = subprocess.run(
                ["bash", "-c", script, "wrapper", "-p", "hi"],
                capture_output=True, text=True, check=True, env=env, cwd=tmp,
            ).stdout
            return dict(line.split("=", 1) for line in out.splitlines()), home

    def test_content_survives_format_env_unchanged(self):
        for account in self.ACCOUNTS:
            with self.subTest(account=account):
                self.assertIn("ci_oauth_token", self._rendered(account))

    def test_a_provisioned_token_file_is_exported(self):
        for account in self.ACCOUNTS:
            with self.subTest(account=account):
                seen, home = self._run(account, "tok-from-file\n")
                self.assertEqual(seen["token"], "tok-from-file")
                self.assertEqual((seen["cwd"], seen["home"], seen["args"]), (home, home, "-p hi"))

    def test_no_token_file_or_an_empty_one_keeps_the_old_behavior(self):
        for account in self.ACCOUNTS:
            for token_text in (None, ""):
                with self.subTest(account=account, token_text=token_text):
                    seen, home = self._run(account, token_text)
                    self.assertEqual(seen["token"], "UNSET")
                    self.assertEqual((seen["cwd"], seen["home"]), (home, home))


class SystemdUnitPathTests(unittest.TestCase):
    """A path in ReadWritePaths= that does not exist stops the service before it starts (systemd
    status 226/NAMESPACE) unless it has a leading "-". The first production release hit this on
    three services (2026-09-21), and it only shows on a fresh server that lacks those directories."""

    def _tokens(self):
        for entry in infra.MANIFEST["static_files"]:
            text = entry.get("content") or ""
            for match in re.finditer(r"^(ReadWritePaths|ReadOnlyPaths|BindPaths)=(.*)$", text, re.M):
                for token in match.group(2).split():
                    yield entry["path"].rsplit("/", 1)[-1], token

    def test_every_required_path_under_opt_hams_is_a_provisioned_directory(self):
        directories = {item["path"] for item in infra.MANIFEST["directories"]}
        missing = [
            (unit, token) for unit, token in self._tokens()
            if token.startswith("/opt/hams") and token not in directories
        ]
        self.assertEqual(missing, [], "these paths are required by a unit but nothing creates them")

    def test_paths_a_fresh_server_lacks_are_marked_optional(self):
        by_unit = {}
        for unit, token in self._tokens():
            by_unit.setdefault(unit, []).append(token)
        backup = by_unit["backup.worker.service"]
        for path in ("/var/lib/odoo/backup_repo", "/var/backups/global", "/opt/hams/backup", "/mnt/backup"):
            self.assertIn("-" + path, backup)
        self.assertIn("-/opt/hams/hams_com/docs/code_review_reports", by_unit["code.review.sweep.service"])
        self.assertTrue(any(item["path"] == "/opt/hams/spool/adif_uploads" for item in infra.MANIFEST["directories"]))


class PostgresqlLockdownTests(unittest.TestCase):
    def test_does_not_loosen_pg_hba_authentication(self):
        # Regression test: this step used to run `sed -i 's/peer/trust/g'`
        # over pg_hba.conf, letting any local OS account connect as any
        # PostgreSQL role, the superuser included. See
        # _postgresql_lockdown_commands()'s docstring.
        commands = infra._postgresql_lockdown_commands()
        self.assertTrue(commands)
        for cmd in commands:
            text = " ".join(cmd)
            self.assertNotIn("pg_hba", text)
            self.assertNotIn("trust", text)

    def test_still_binds_postgresql_to_loopback(self):
        text = "\n".join(" ".join(cmd) for cmd in infra._postgresql_lockdown_commands())
        self.assertIn("listen_addresses = '127.0.0.1, ::1'", text)
        self.assertIn("shared_preload_libraries = 'pg_stat_statements'", text)

    def test_provision_environment_uses_the_lockdown_commands_and_no_pg_hba_edit(self):
        # provision_environment() itself is too host-dependent to execute
        # here (see this file's docstring), so check its source: the
        # blanket substitution must not come back inline.
        source = inspect.getsource(infra.provision_environment)
        self.assertIn("_postgresql_lockdown_commands()", source)
        self.assertNotIn("pg_hba", source)
        self.assertNotIn("s/peer/trust", source)


class ProvisionEnvironmentHookWiringTests(unittest.TestCase):
    """provision_environment() itself is too host-dependent to execute here
    (see this file's docstring, and PostgresqlLockdownTests'
    test_provision_environment_uses_the_lockdown_commands_and_no_pg_hba_edit
    above for the established pattern this follows), so this checks its
    source instead of running it. Bug found 2026-09-18: execute_hooks() was
    defined and covered by ExecuteHooksTests above in isolation, but nothing
    in provision_environment (or anywhere else in production) ever called
    it, so hook_generate_ssl/hook_clear_pycache silently never ran during a
    real provisioning run despite being attached to MANIFEST["directories"]
    entries via post_provision_hooks."""

    def test_provision_environment_calls_execute_hooks_for_prod_and_test(self):
        source = inspect.getsource(infra.provision_environment)
        self.assertIn('execute_hooks("prod", run_cmd_func, env_vars)', source)
        self.assertIn('execute_hooks("test", run_cmd_func, env_vars)', source)

    def test_execute_hooks_runs_after_the_directories_it_hooks_against_are_created(self):
        # apply_production_directories() is what actually creates the
        # MANIFEST["directories"] paths on disk -- execute_hooks() must run
        # after it, not before, or the hooks would fire against
        # directories that don't exist yet.
        source = inspect.getsource(infra.provision_environment)
        dirs_prod_idx = source.index(
            'apply_production_directories(run_cmd_func, environment="prod")'
        )
        hooks_prod_idx = source.index('execute_hooks("prod", run_cmd_func, env_vars)')
        self.assertLess(dirs_prod_idx, hooks_prod_idx)

        dirs_test_idx = source.index(
            'apply_production_directories(run_cmd_func, environment="test")'
        )
        hooks_test_idx = source.index('execute_hooks("test", run_cmd_func, env_vars)')
        self.assertLess(dirs_test_idx, hooks_test_idx)

class LocalDatabaseBackupUnitTests(_TmpDirTestCase):
    """The nightly local pg_dump unit (interim safety net while no S3/B2 backup is configured): its shell command must
    parse, write a private dump, and keep only the seven newest."""

    def _unit(self, suffix):
        return next(
            item for item in infra.MANIFEST["static_files"]
            if item.get("path") == f"/opt/hams/systemd/hams.db.local.backup.{suffix}"
        )

    def _command(self):
        text = infra.format_env(self._unit("service")["content"], {})
        line = next(l for l in text.splitlines() if l.startswith("ExecStart="))
        argv = shlex.split(line[len("ExecStart="):])
        self.assertEqual(argv[:2], ["/bin/bash", "-c"])
        # systemd itself expands a bare $NAME (to nothing, when unset), so a literal dollar sign must be written $$.
        # It also turns %% into %. Do what systemd does so the script runs as it would there.
        self.assertNotRegex(argv[2].replace("$$", ""), r"\$", "an unescaped $ would be eaten by systemd")
        return argv[2].replace("$$", "$").replace("%%", "%")

    def test_it_is_a_production_only_daily_timer_for_the_service(self):
        self.assertEqual(self._unit("service")["environments"], ["prod"])
        self.assertEqual(self._unit("timer")["environments"], ["prod"])
        self.assertIn("OnCalendar=*-*-* 02:30:00", self._unit("timer")["content"])
        self.assertIn("Persistent=true", self._unit("timer")["content"])

    def test_the_command_writes_a_private_dump_and_keeps_only_the_seven_newest(self):
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        counter = os.path.join(self.tmp, "counter")
        with open(os.path.join(bin_dir, "runuser"), "w") as f:
            f.write("#!/bin/bash\necho dump-of-\"$@\"\n")
        # A stub clock so ten runs in one second still get ten different file names, and increasing mtimes.
        with open(os.path.join(bin_dir, "date"), "w") as f:
            f.write(f"#!/bin/bash\nn=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {counter}; printf '2026-01-01-%04d' $n\n")
        for name in ("runuser", "date"):
            os.chmod(os.path.join(bin_dir, name), 0o755)
        backup_dir = os.path.join(self.tmp, "db-daily")
        command = self._command().replace("/opt/hams/backups/db-daily", backup_dir)
        self.assertNotIn("/opt/hams", command)
        env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"])
        for _ in range(10):
            subprocess.run(["/bin/bash", "-c", command], env=env, check=True)
            # Give the files distinct, increasing mtimes without sleeping; retention orders by mtime.
            for i, name in enumerate(sorted(os.listdir(backup_dir))):
                os.utime(os.path.join(backup_dir, name), (1000 + i, 1000 + i))
        kept = sorted(os.listdir(backup_dir))
        self.assertEqual(len(kept), 7)
        self.assertEqual(kept[-1], "hams_prod-2026-01-01-0010.dump")
        self.assertEqual(kept[0], "hams_prod-2026-01-01-0004.dump")
        self.assertFalse([n for n in kept if n.endswith(".tmp")])
        self.assertEqual(os.stat(os.path.join(backup_dir, kept[-1])).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(backup_dir).st_mode & 0o777, 0o700)
        with open(os.path.join(backup_dir, kept[-1])) as f:
            self.assertIn("pg_dump -Fc hams_prod", f.read())

    def test_a_failing_dump_leaves_no_partial_file_and_fails_the_unit(self):
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "runuser"), "w") as f:
            f.write("#!/bin/bash\necho half-a-dump\nexit 3\n")
        os.chmod(os.path.join(bin_dir, "runuser"), 0o755)
        backup_dir = os.path.join(self.tmp, "db-daily")
        command = self._command().replace("/opt/hams/backups/db-daily", backup_dir)
        env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"])
        result = subprocess.run(["/bin/bash", "-c", command], env=env, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([n for n in os.listdir(backup_dir) if n.endswith(".dump")], [])

class TimerDrivenUnitTests(unittest.TestCase):
    """A sync that has a timer runs once and exits, so its service must be Type=oneshot and must not carry
    Restart=always. ncvec.sync and au.pii.sync were Type=simple with Restart=always and RestartSec=60 while their main
    scripts run one pass and exit, so systemd restarted them every minute for days (8,247 and 7,738 restarts on hams1),
    re-scraping ncvec.org once a minute."""

    def _units(self):
        # Comment lines removed: several units explain, in a comment, the restart loop they used to have.
        return {
            item["path"].rsplit("/", 1)[1]: "\n".join(
                line for line in item["content"].splitlines() if not line.lstrip().startswith("#")
            )
            for item in infra.MANIFEST["static_files"]
            if item.get("path", "").startswith("/opt/hams/systemd/")
        }

    def test_every_service_that_has_a_timer_is_a_oneshot_that_is_not_restarted(self):
        units = self._units()
        timers = [name for name in units if name.endswith(".timer")]
        self.assertGreater(len(timers), 10)
        for timer in timers:
            service = timer[: -len(".timer")] + ".service"
            self.assertIn(service, units, f"{timer} has no service to run")
            self.assertIn("Type=oneshot", units[service], f"{service} is started by a timer, so it must be oneshot")
            self.assertNotIn("Restart=always", units[service], f"{service} must not restart itself under a timer")

    def test_ncvec_sync_writes_its_data_under_the_writable_spool_not_the_read_only_source_tree(self):
        # ProtectSystem=strict makes /opt/hams/daemons read-only; the daemon's default of ../../ham_training/data failed
        # every cycle with "Read-only file system".
        unit = self._units()["ncvec.sync.service"]
        self.assertIn('Environment="HAMS_NCVEC_DATA_DIR=/opt/hams/spool/ncvec"', unit)
        self.assertIn("ReadWritePaths=/opt/hams/spool", unit)

    def test_ncvec_and_au_pii_are_now_timer_driven(self):
        units = self._units()
        for name in ("ncvec.sync", "au.pii.sync"):
            self.assertIn(f"{name}.timer", units)
            self.assertIn("OnCalendar=daily", units[f"{name}.timer"])
            self.assertIn("WantedBy=timers.target", units[f"{name}.timer"])
            self.assertNotIn("WantedBy=multi-user.target", units[f"{name}.service"])

    def test_uk_ofcom_sync_polls_weekly_not_daily(self):
        # Ofcom's amateur callsign CSV changes rarely; a daily poll of someone else's server for an
        # almost-always-unchanged file is load for nothing (2026-10-02).
        timer = self._units()["uk.ofcom.sync.timer"]
        self.assertIn("OnCalendar=weekly", timer)
        self.assertNotIn("OnCalendar=daily", timer)
        self.assertIn("Persistent=true", timer)

    def test_callbook_dns_export_follows_each_country_sync(self):
        # docs/proposals/CALLBOOK_DNS_SERVICE.md (hams_com), "Serving": the zone is rebuilt after
        # each sync of a country it publishes. CA and AU sync on this host and chain with OnSuccess=;
        # fcc.uls.sync runs on pi500-1 (05:00 America/New_York + up to 1h), so the export timer has
        # its own run two hours after that window, in the same time zone.
        units = self._units()
        export = "OnSuccess=callbook.dns.export.service"
        for name in ("ised.canada.sync.service", "au.acma.sync.service"):
            self.assertIn(export, units[name], f"{name} must trigger the callbook DNS export")
        self.assertIn("callbook.dns.export.service", units)
        timer = units["callbook.dns.export.timer"]
        self.assertIn("OnCalendar=daily", timer)
        self.assertIn("OnCalendar=*-*-* 08:00:00 America/New_York", timer)
        fcc_timer = next(
            item["content"] for item in infra.MANIFEST["static_files"]
            if item.get("path") == "/etc/systemd/system/fcc.uls.sync.timer" and "pi500-1" in item["environments"]
        )
        self.assertIn("OnCalendar=*-*-* 05:00:00", fcc_timer)
        self.assertIn("RandomizedDelaySec=1h", fcc_timer)


class SigningKeyMigrationHookTests(_TmpDirTestCase):
    """Tests [@ANCHOR: infrastructure:migrate_signing_key]

    hams_com's relay_signer/subcarrier_signer/device_command_signer daemons
    take over keys that older Odoo code kept in /var/lib/odoo. The hooks run as root
    in production; here the account lookups resolve to the test runner's
    own uid/gid, so the real fchown/rename/remove path runs unpatched."""

    KEY_NAME = "hams_subcarrier_signing_ed25519.key"
    OLD_MTIME = 1_700_000_000

    def setUp(self):
        super().setUp()
        self.old_dir = os.path.join(self.tmp, "var/lib/odoo")
        self.new_dir = os.path.join(self.tmp, "opt/hams/etc/subcarrier_signer")
        os.makedirs(self.old_dir)
        os.makedirs(self.new_dir)
        self.old_path = os.path.join(self.old_dir, self.KEY_NAME)
        self.new_path = os.path.join(self.new_dir, self.KEY_NAME)
        self.uid = os.getuid()
        self.gid = os.getgid()
        self.safe_patch(
            "infrastructure.pwd.getpwnam",
            return_value=MagicMock(pw_uid=self.uid),
        )
        self.safe_patch(
            "infrastructure.grp.getgrnam",
            return_value=MagicMock(gr_gid=self.gid),
        )

    def _write_old_key(self, raw=b"k" * 32):
        with open(self.old_path, "wb") as f:
            f.write(raw)
        os.chmod(self.old_path, 0o600)
        os.utime(self.old_path, (self.OLD_MTIME, self.OLD_MTIME))
        return raw

    def _run_hook(self):
        infra.hook_migrate_subcarrier_signing_key({}, self.tmp, self.new_dir, None)

    def test_moves_the_key_preserving_bytes_mode_and_mtime(self):
        raw = self._write_old_key()
        self._run_hook()
        self.assertFalse(os.path.exists(self.old_path))
        with open(self.new_path, "rb") as f:
            self.assertEqual(f.read(), raw)
        st = os.stat(self.new_path)
        self.assertEqual(st.st_mode & 0o777, 0o600)
        self.assertEqual(int(st.st_mtime), self.OLD_MTIME)
        self.assertEqual((st.st_uid, st.st_gid), (self.uid, self.gid))
        self.assertEqual(os.listdir(self.new_dir), [self.KEY_NAME])
        self.assertEqual(infra.get_hook_failures(), [])

    def test_no_old_key_is_a_no_op(self):
        self._run_hook()
        self.assertEqual(os.listdir(self.new_dir), [])
        self.assertEqual(infra.get_hook_failures(), [])

    def test_identical_leftover_old_key_is_removed(self):
        raw = self._write_old_key()
        with open(self.new_path, "wb") as f:
            f.write(raw)
        self._run_hook()
        self.assertFalse(os.path.exists(self.old_path))
        self.assertEqual(infra.get_hook_failures(), [])

    def test_different_old_key_is_kept_and_reported(self):
        self._write_old_key(b"o" * 32)
        with open(self.new_path, "wb") as f:
            f.write(b"n" * 32)
        self._run_hook()
        self.assertTrue(os.path.exists(self.old_path))
        with open(self.new_path, "rb") as f:
            self.assertEqual(f.read(), b"n" * 32)
        failures = infra.get_hook_failures()
        self.assertEqual(
            [name for name, _ in failures], ["hook_migrate_subcarrier_signing_key"]
        )

    def test_missing_account_keeps_the_old_key(self):
        self._write_old_key()
        self.safe_patch("infrastructure.pwd.getpwnam", side_effect=KeyError("nope"))
        self._run_hook()
        self.assertTrue(os.path.exists(self.old_path))
        self.assertEqual(os.listdir(self.new_dir), [])
        self.assertEqual(len(infra.get_hook_failures()), 1)

    @unittest.skipIf(os.geteuid() == 0, "root can chown to any uid")
    def test_failed_chown_keeps_the_old_key_and_leaves_no_partial_copy(self):
        self._write_old_key()
        self.safe_patch(
            "infrastructure.pwd.getpwnam", return_value=MagicMock(pw_uid=0)
        )
        self._run_hook()
        self.assertTrue(os.path.exists(self.old_path))
        self.assertEqual(os.listdir(self.new_dir), [])
        self.assertEqual(len(infra.get_hook_failures()), 1)

    def test_device_command_hook_uses_its_own_key_and_account(self):
        key_name = "hams_device_command_signing_ed25519.key"
        old_path = os.path.join(self.old_dir, key_name)
        with open(old_path, "wb") as f:
            f.write(b"d" * 32)
        getpwnam = infra.pwd.getpwnam
        infra.hook_migrate_device_command_signing_key({}, self.tmp, self.new_dir, None)
        self.assertFalse(os.path.exists(old_path))
        self.assertTrue(os.path.exists(os.path.join(self.new_dir, key_name)))
        getpwnam.assert_called_with("hams_device_command_signer")

    def test_relay_hook_moves_the_noise_key_to_its_own_account(self):
        # The relay key's file name is the historical
        # hams_noise_signing_ed25519.key (daemons/relay_signer/main.py's
        # SIGNING_KEY_PATH), not hams_relay_signing_ed25519.key.
        key_name = "hams_noise_signing_ed25519.key"
        old_path = os.path.join(self.old_dir, key_name)
        with open(old_path, "wb") as f:
            f.write(b"r" * 32)
        os.utime(old_path, (self.OLD_MTIME, self.OLD_MTIME))
        getpwnam = infra.pwd.getpwnam
        infra.hook_migrate_relay_signing_key({}, self.tmp, self.new_dir, None)
        self.assertFalse(os.path.exists(old_path))
        new_path = os.path.join(self.new_dir, key_name)
        with open(new_path, "rb") as f:
            self.assertEqual(f.read(), b"r" * 32)
        self.assertEqual(int(os.stat(new_path).st_mtime), self.OLD_MTIME)
        getpwnam.assert_called_with("hams_relay_signer")
        self.assertEqual(infra.get_hook_failures(), [])


class SignerDaemonManifestTests(unittest.TestCase):
    """The MANIFEST pieces hams_com's three signer daemons need to start at
    all. The defaults below are copied from each daemon's own main.py
    (BASE_DIR/PUBLIC_DIR/SOCKET_PATH) and its Odoo-side client."""

    SIGNERS = {
        "subcarrier_signer": ("subcarrier-signer", "SUBCARRIER_SIGNER"),
        "device_command_signer": ("device-command-signer", "DEVICE_COMMAND_SIGNER"),
        "relay_signer": ("relay-signer", "RELAY_SIGNER"),
    }

    def _unit(self, unit_name):
        for entry in infra.MANIFEST["static_files"]:
            if entry["path"] == f"/opt/hams/systemd/hams-{unit_name}.service":
                return entry["content"]
        self.fail(f"no hams-{unit_name}.service in static_files")

    def test_accounts_exist_and_odoo_joins_each_group(self):
        accounts = {a["user"]: a for a in infra.MANIFEST["system_accounts"]}
        for name in self.SIGNERS:
            account = accounts[f"hams_{name}"]
            self.assertEqual(account["group"], f"hams_{name}")
            self.assertEqual(account["shell"], "/usr/sbin/nologin")
            self.assertEqual(account["add_to_users"], ["odoo"])
            self.assertEqual(account["environments"], ["prod", "test"])

    def test_private_dir_is_0700_and_public_dir_is_0755(self):
        dirs = {d["path"]: d for d in infra.MANIFEST["directories"]}
        hooks = {
            "subcarrier_signer": infra.hook_migrate_subcarrier_signing_key,
            "device_command_signer": infra.hook_migrate_device_command_signing_key,
            "relay_signer": infra.hook_migrate_relay_signing_key,
        }
        for name in self.SIGNERS:
            owner = f"hams_{name}:hams_{name}"
            private = dirs[f"/opt/hams/etc/{name}"]
            public = dirs[f"/opt/hams/etc/{name}_public"]
            self.assertEqual((private["owner"], private["provision_mode"]), (owner, "700"))
            self.assertEqual((public["owner"], public["provision_mode"]), (owner, "755"))
            self.assertEqual(private["post_provision_hooks"], [hooks[name]])

    def test_unit_can_start_and_odoo_can_reach_its_socket(self):
        for name, (unit_name, env_prefix) in self.SIGNERS.items():
            unit = self._unit(unit_name)
            self.assertTrue(unit.startswith("[Unit]\n"))
            self.assertIn(f"User=hams_{name}\n", unit)
            # /opt/hams and /opt/hams/etc are 0750 hams_com.
            self.assertIn("SupplementaryGroups=hams_com\n", unit)
            # --start-test writes the public key, so both dirs are writable.
            self.assertIn(
                f"ReadWritePaths=/opt/hams/etc/{name} /opt/hams/etc/{name}_public\n",
                unit,
            )
            self.assertIn(f"RuntimeDirectory=hams_{name}\n", unit)
            self.assertIn("RuntimeDirectoryMode=0750\n", unit)
            expected_env = {
                "BASE_DIR": f"/opt/hams/etc/{name}",
                "PUBLIC_DIR": f"/opt/hams/etc/{name}_public",
                "SOCKET_PATH": f"/run/hams_{name}/signer.sock",
            }
            for key, value in expected_env.items():
                self.assertIn(f'Environment="{env_prefix}_{key}={value}"\n', unit)
            main_py = f"/opt/hams/daemons/{name}/main.py"
            self.assertIn(f"ExecStartPre=/usr/bin/python3 {main_py} --start-test\n", unit)
            self.assertIn(f"ExecStart=/usr/bin/python3 {main_py}\n", unit)
            self.assertIn("WantedBy=multi-user.target\n", unit)



class ExternalFetchUnitClassificationTests(unittest.TestCase):
    """Tests [@ANCHOR: infrastructure:external_fetch_unit_names]

    Standing rule (2026-10-03): a test or development machine never runs a unit that fetches
    from third-party servers. On 2026-10-02 `provision.py --test` enabled every timer and its
    smoketest started every service on fresh test hosts; one of them pulled the whole national
    licence database. The MANIFEST now marks each such unit "external_fetch". This class forces
    every systemd unit in the MANIFEST into exactly one of two audited buckets, so a new unit
    cannot be added without someone deciding which it is (by reading its daemon's code)."""

    # Read each daemon's code, 2026-10-02: none of these talks to a third-party server on its own.
    AUDITED_LOCAL_ONLY_UNITS = {
        "adif.ingress.service": "HTTP upload endpoint; only calls the local Odoo and RabbitMQ",
        "adif.processor.service": "RabbitMQ consumer; local Odoo only",
        "backup.worker.service": "runs only backup jobs an operator configured; idle otherwise",
        "callbook.dns.export.service": "reads Odoo, writes the local PowerDNS zone",
        "callbook.dns.export.timer": "activates callbook.dns.export.service (local)",
        "callbook.dns.rrl.service": "local DNS rate-limiting proxy",
        "dx.firehose.service": "local websocket server over the local database",
        "gdpr.csv.export.service": "local HTTP export server",
        "hamcall.idx.sync.service": "reads a licensed file already on disk; no timer",
        "hams-auth-gateway.service": "local server",
        "hams-device-command-signer.service": "local signing socket",
        "hams-pgbackrest-backup.path": "fires only on a spool file a configured backup job writes",
        "hams-pgbackrest-backup.service": "runs only for a configured backup job",
        "hams-pycache.service": "compiles local Python files",
        "hams-relay-signer.service": "local signing socket",
        "hams-subcarrier-signer.service": "local signing socket",
        "hams.daemon.keys.service": "provisions daemon keys in the local database",
        "hams.data.relay.service": "serves map data from local Redis; no ingestion task",
        "hams.db.local.backup.service": "local pg_dump",
        "hams.db.local.backup.timer": "activates hams.db.local.backup.service (local)",
        "hams.relay.bridge.service": "local websocket bridge to the local Odoo",
        "hams.simulated.band.service": "local server; STUN only while negotiating with a peer",
        "hams.simulated.bots.service": "local band client; speech model fetched once into a cache",
        "hams.simulated.observer.service": "local band client, same as the bots",
        "pdns.callbook.service": "local PowerDNS server",
        "pdns.sync.service": "RabbitMQ consumer writing to the local PowerDNS API",
        "stray.odoo.shell.detector.service": "inspects local processes",
        "stray.odoo.shell.detector.timer": "activates stray.odoo.shell.detector.service (local)",
    }

    # Units seen running on test hosts in the 2026-10-02 incident; each must stay classified.
    INCIDENT_UNITS = (
        "fcc.uls.sync.service",
        "fcc.uls.sync.timer",
        "uk.ofcom.sync.timer",
        "au.acma.sync.timer",
        "de.bnetza.sync.timer",
        "br.anatel.sync.timer",
        "pota.sync.timer",
        "sota.sync.timer",
        "wa7bnm.contest.sync.timer",
        "arrl.hamfests.sync.timer",
        "qrz.scraper.service",
    )

    def _unit_specs(self):
        return [
            spec for spec in infra.MANIFEST["static_files"]
            if "/systemd/" in spec["path"]
            and spec["path"].endswith((".service", ".timer", ".path"))
        ]

    def test_every_systemd_unit_is_classified_exactly_once(self):
        flagged = infra.external_fetch_unit_names()
        for spec in self._unit_specs():
            name = os.path.basename(spec["path"])
            in_local = name in self.AUDITED_LOCAL_ONLY_UNITS
            self.assertNotEqual(
                name in flagged,
                in_local,
                f"{name} must be either MANIFEST external_fetch or listed in "
                "AUDITED_LOCAL_ONLY_UNITS, not both and not neither -- read its daemon's code",
            )

    def test_audited_local_set_has_no_stale_names(self):
        names = {os.path.basename(spec["path"]) for spec in self._unit_specs()}
        stale = set(self.AUDITED_LOCAL_ONLY_UNITS) - names
        self.assertEqual(stale, set())

    def test_every_flag_is_a_non_empty_reason(self):
        for spec in infra.MANIFEST["static_files"]:
            if "external_fetch" in spec:
                self.assertIsInstance(spec["external_fetch"], str, spec["path"])
                self.assertTrue(spec["external_fetch"].strip(), spec["path"])

    def test_a_timer_and_its_service_are_classified_together(self):
        flagged = infra.external_fetch_unit_names()
        names = {os.path.basename(spec["path"]) for spec in self._unit_specs()}
        for name in names:
            if not name.endswith(".timer"):
                continue
            service = name[: -len(".timer")] + ".service"
            self.assertIn(service, names, name)
            self.assertEqual(name in flagged, service in flagged, f"{name} vs {service}")

    def test_every_copy_of_a_unit_carries_the_same_flag(self):
        # fcc.uls.sync has a second pair of entries for its dedicated egress host.
        seen = {}
        for spec in self._unit_specs():
            name = os.path.basename(spec["path"])
            seen.setdefault(name, set()).add(bool(spec.get("external_fetch")))
        for name, flags in seen.items():
            self.assertEqual(len(flags), 1, f"{name} is flagged in one entry but not another")

    def test_incident_units_are_classified_external(self):
        flagged = infra.external_fetch_unit_names()
        for name in self.INCIDENT_UNITS:
            self.assertIn(name, flagged)

    def test_system_startup_is_classified_because_it_starts_an_external_sync(self):
        # Its ExecStart is `systemctl start amsat.tle.sync.service`, and the smoketest used to
        # start it in test mode only (the daemons_to_skip check skips it in production).
        unit = next(
            spec for spec in infra.MANIFEST["static_files"]
            if spec["path"].endswith("/system-startup.service")
        )
        self.assertIn("amsat.tle.sync.service", unit["content"])
        self.assertIn("amsat.tle.sync.service", infra.external_fetch_unit_names())
        self.assertIn("system-startup.service", infra.external_fetch_unit_names())


class ExternalFetchUnitsNeverActivatedInTestTests(unittest.TestCase):
    """Tests [@ANCHOR: infrastructure:activation_units_to_enable]
    Tests [@ANCHOR: infrastructure:smoketest_candidate_services]

    Fails if any external-fetch unit would be enabled or started in a test environment, and
    also if the flag silently dropped one from production."""

    def _linked_activation_units(self):
        # Same selection provision_environment()'s linking loop makes from /opt/hams/systemd.
        return sorted(
            {
                os.path.basename(spec["path"])
                for spec in infra.MANIFEST["static_files"]
                if spec["path"].startswith("/opt/hams/systemd/")
                and spec["path"].endswith((".timer", ".path"))
            }
        )

    def test_no_external_fetch_unit_is_enabled_in_a_test_environment(self):
        linked = self._linked_activation_units()
        enabled = infra._activation_units_to_enable(linked, is_test_env=True)
        flagged = infra.external_fetch_unit_names()
        self.assertEqual(set(enabled) & flagged, set())
        self.assertTrue(set(linked) & flagged, "expected some flagged timers to be linked")
        # Local timers are still enabled in test.
        self.assertIn("stray.odoo.shell.detector.timer", enabled)

    def test_production_still_enables_every_linked_unit(self):
        linked = self._linked_activation_units()
        self.assertEqual(infra._activation_units_to_enable(linked, is_test_env=False), linked)

    def test_provision_environment_enables_only_the_filtered_list(self):
        source = inspect.getsource(infra.provision_environment)
        self.assertIn("_activation_units_to_enable(", source)
        self.assertIn("linked_activation_units, is_test_env", source)
        self.assertIn("for unit in units_to_enable:", source)
        self.assertNotIn("for unit in linked_activation_units:", source)

    def test_smoketest_never_lists_an_external_fetch_service_in_a_test_environment(self):
        flagged = infra.external_fetch_unit_names()
        for has_hams_com in (True, False):
            candidates = infra._smoketest_candidate_services(has_hams_com, is_test_env=True)
            self.assertEqual(set(candidates) & flagged, set())
        candidates = infra._smoketest_candidate_services(True, is_test_env=True)
        self.assertIn("adif.processor.service", candidates)

    def test_smoketest_still_starts_external_fetch_services_in_production(self):
        candidates = infra._smoketest_candidate_services(True, is_test_env=False)
        self.assertIn("qrz.scraper.service", candidates)
        self.assertIn("fcc.uls.sync.service", candidates)
        self.assertNotIn("system-startup.service", candidates)


class SmoketestTestModeTests(_SafePatchTestCase):
    """Tests [@ANCHOR: infrastructure:smoketest_test_mode_always_stops]

    Runs the real run_post_provision_smoketest() with systemctl replaced by a recorder."""

    FAILING_SERVICE = "gdpr.csv.export.service"

    def _fake_systemctl(self, calls, fail_service=None):
        def fake_run(cmd, *args, **kwargs):
            calls.append(list(cmd))
            stdout = ""
            returncode = 0
            if cmd[:2] == ["systemctl", "is-active"]:
                stdout = "inactive\n"
            elif cmd[:2] == ["systemctl", "is-failed"]:
                stdout = "active\n"
            elif cmd[:2] == ["systemctl", "start"] and cmd[-1] == fail_service:
                returncode = 1
            return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

        return fake_run

    def _run(self, is_test_env, fail_service=None):
        calls = []
        self.safe_patch_object(
            infra.subprocess, "run", side_effect=self._fake_systemctl(calls, fail_service)
        )
        self.safe_patch_object(infra.time, "sleep")
        exited = False
        try:
            infra.run_post_provision_smoketest(has_hams_com=True, is_test_env=is_test_env)
        except SystemExit:
            exited = True
        return calls, exited

    @staticmethod
    def _started(calls):
        return [cmd[-1] for cmd in calls if cmd[:2] == ["systemctl", "start"]]

    @staticmethod
    def _stopped(calls):
        return [cmd[-1] for cmd in calls if cmd[:2] == ["systemctl", "stop"]]

    def test_test_mode_never_starts_an_external_fetch_service(self):
        calls, exited = self._run(is_test_env=True)
        self.assertFalse(exited)
        started = self._started(calls)
        self.assertTrue(started)
        self.assertEqual(set(started) & infra.external_fetch_unit_names(), set())

    def test_test_mode_stops_what_it_started_on_success(self):
        calls, _ = self._run(is_test_env=True)
        self.assertEqual(sorted(self._stopped(calls)), sorted(self._started(calls)))

    def test_test_mode_stops_what_it_started_even_when_a_start_fails(self):
        calls, exited = self._run(is_test_env=True, fail_service=self.FAILING_SERVICE)
        self.assertTrue(exited, "a failed start must still fail the smoketest")
        started = self._started(calls)
        self.assertIn(self.FAILING_SERVICE, started)
        self.assertEqual(sorted(self._stopped(calls)), sorted(started))

    def test_production_starts_external_fetch_services_and_leaves_them_running(self):
        calls, exited = self._run(is_test_env=False)
        self.assertFalse(exited)
        self.assertIn("qrz.scraper.service", self._started(calls))
        self.assertEqual(self._stopped(calls), [])


class SharedOdooAccountRatchetTests(unittest.TestCase):
    """Tests [@ANCHOR: infrastructure:shared_odoo_account_ratchet]"""

    def test_every_unit_running_as_odoo_is_on_the_reviewed_list(self):
        running_as_odoo = infra.systemd_units_running_as(infra.MANIFEST, "odoo")
        unlisted = sorted(running_as_odoo - infra.SHARED_ODOO_ACCOUNT_UNITS)
        self.assertEqual(
            unlisted, [],
            "These units run as User=odoo and so can read every other daemon's key file. Give "
            "each its own account, or add it to SHARED_ODOO_ACCOUNT_UNITS in a reviewed change.",
        )

    def test_no_listed_unit_has_already_moved_off_the_odoo_account(self):
        running_as_odoo = infra.systemd_units_running_as(infra.MANIFEST, "odoo")
        stale = sorted(infra.SHARED_ODOO_ACCOUNT_UNITS - running_as_odoo)
        self.assertEqual(
            stale, [],
            "These units no longer run as User=odoo; remove them from SHARED_ODOO_ACCOUNT_UNITS.",
        )

    def test_the_units_with_their_own_account_are_found_under_that_account(self):
        # The same parser must see the existing dedicated-account units, or the two checks
        # above could pass on a parser that finds nothing.
        self.assertIn(
            "localhost.cert.renewal.service",
            infra.systemd_units_running_as(infra.MANIFEST, "localhost_cert"),
        )
        self.assertNotIn(
            "localhost.cert.renewal.service",
            infra.systemd_units_running_as(infra.MANIFEST, "odoo"),
        )


if __name__ == "__main__":
    unittest.main()
