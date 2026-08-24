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
import os
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


def _write_os_release(path, id_line=None, codename_line=None):
    with open(path, "w") as f:
        if id_line is not None:
            f.write(f'ID="{id_line}"\n')
        if codename_line is not None:
            f.write(f'VERSION_CODENAME={codename_line}\n')


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

    def test_a_missing_variable_returns_the_original_text_unformatted(self):
        # KeyError is caught -- the format template is returned as-is
        # rather than raising, since a hook with an unresolved
        # placeholder shouldn't crash the whole provisioning run.
        self.assertEqual(infra.format_env("host={MISSING}", {}), "host={MISSING}")


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
    def test_extracts_and_chmods_the_kopia_binary_via_run_cmd_func(self):
        archive_path = os.path.join(self.tmp, "kopia.tar.gz")
        with open(archive_path, "w") as f:
            f.write("fake archive bytes")

        mock_run = MagicMock()
        infra.hook_install_kopia_binary({}, self.tmp, archive_path, mock_run)

        target_dir = os.path.join(self.tmp, "usr", "bin")
        self.assertEqual(mock_run.call_count, 2)
        extract_cmd = mock_run.call_args_list[0][0][0]
        self.assertIn("tar", extract_cmd)
        self.assertIn(target_dir, extract_cmd)
        chmod_cmd = mock_run.call_args_list[1][0][0]
        self.assertEqual(chmod_cmd, ["chmod", "+x", os.path.join(target_dir, "kopia")])
        self.assertFalse(os.path.exists(archive_path))

    def test_a_run_cmd_failure_is_swallowed_and_the_archive_is_still_cleaned_up(self):
        archive_path = os.path.join(self.tmp, "kopia.tar.gz")
        with open(archive_path, "w") as f:
            f.write("fake archive bytes")
        mock_run = MagicMock(side_effect=RuntimeError("simulated tar failure"))
        infra.hook_install_kopia_binary({}, self.tmp, archive_path, mock_run)  # must not raise
        self.assertFalse(os.path.exists(archive_path))


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


if __name__ == "__main__":
    unittest.main()
