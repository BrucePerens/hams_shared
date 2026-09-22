#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
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
import inspect
import os
import shlex
import shutil
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

        def fake_run_cmd(cmd):
            with open(fullchain, "w") as f:
                f.write("cert")
            with open(os.path.join(ssl_dir, "privkey.pem"), "w") as f:
                f.write("key")

        mock_run = MagicMock(side_effect=fake_run_cmd)

        infra.execute_hooks("prod", mock_run, {"DOMAIN": "hams.com"}, dest_dir=self.tmp)

        # hook_generate_ssl really ran, against the real, correctly
        # dest_dir-joined MANIFEST directory path.
        mock_run.assert_called_once()
        self.assertIn("openssl", mock_run.call_args[0][0])
        self.assertTrue(os.path.exists(os.path.join(ssl_dir, "lotw_root.pem")))

        # hook_clear_pycache really ran too.
        self.assertEqual(os.listdir(pycache_dir), [])

    def test_docker_environment_only_touches_deploy_ssl_not_opt_hams_nginx(self):
        deploy_ssl = os.path.join(self.tmp, "deploy/ssl")
        os.makedirs(deploy_ssl)
        fullchain = os.path.join(deploy_ssl, "fullchain.pem")

        def fake_run_cmd(cmd):
            with open(fullchain, "w") as f:
                f.write("cert")
            with open(os.path.join(deploy_ssl, "privkey.pem"), "w") as f:
                f.write("key")

        mock_run = MagicMock(side_effect=fake_run_cmd)
        infra.execute_hooks("docker", mock_run, {}, dest_dir=self.tmp)

        self.assertTrue(os.path.exists(os.path.join(deploy_ssl, "lotw_root.pem")))
        # /opt/hams/nginx/ssl's directory entry is prod-only -- "docker"
        # must not touch it.
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "opt/hams/nginx/ssl")))

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

    def test_skips_creation_entirely_when_the_role_already_exists(self):
        mock_run_cmd_func = self._run("normalPass123", role_already_exists=True)
        mock_run_cmd_func.assert_not_called()


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


class SystemdUnitPathTests(unittest.TestCase):
    """A path in ReadWritePaths= that does not exist stops the service before it starts (systemd
    status 226/NAMESPACE) unless it has a leading "-". The first production release hit this on
    three services (2026-09-21), and it only shows on a fresh server that lacks those directories."""

    def _tokens(self):
        import re
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


if __name__ == "__main__":
    unittest.main()
