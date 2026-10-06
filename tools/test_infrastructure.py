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
import json
import os
import re
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import b2_backup
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


class HonestUserAgentDefaultTests(_SafePatchTestCase):
    """ADR 0104: the provisioned and fallback User-Agent is URL-only, with no personal contact."""

    HONEST = "HamsComSyncDaemon/1.0 (+https://crawler.hams.com)"

    def test_download_file_default_user_agent_is_the_honest_string(self):
        captured = {}

        def fake_urlopen(req, timeout=5):
            captured["ua"] = req.headers.get("User-agent")
            m = MagicMock()
            m.read.return_value = b""
            m.__enter__.return_value = m
            m.__exit__.return_value = False
            return m

        self.safe_patch_object(infra.urllib.request, "urlopen", side_effect=fake_urlopen)
        with tempfile.TemporaryDirectory() as tmp:
            infra.download_file("https://example.invalid/file", os.path.join(tmp, "f"), 0o644, {})
        self.assertEqual(captured["ua"], self.HONEST)

    def test_every_system_user_agent_literal_in_this_module_is_the_honest_string(self):
        source = inspect.getsource(infra)
        literals = re.findall(r"SYSTEM_USER_AGENT=([^\"\n]+)", source)
        literals += re.findall(r"(?:get|setdefault)\(\s*\"SYSTEM_USER_AGENT\",\s*\"([^\"]+)\"", source)
        self.assertGreaterEqual(len(literals), 3)
        for literal in literals:
            self.assertEqual(literal, self.HONEST)
            self.assertNotRegex(literal, r"@|(?i:bruce|perens)|\d{3}[ .-]\d{3}[ .-]\d{4}")


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


class PdnsPublicConfigTests(_TmpDirTestCase):
    """The one PowerDNS that owns port 53 has no rate limiter in front of it (Bruce, 2026-10-04), so
    its shipped configuration is what keeps it from being an amplifier or an open resolver. The
    first tests read the MANIFEST; the last start a real pdns_server with exactly that file."""

    SCHEMA = infra._PDNS_SQLITE_SCHEMA_PATH
    TYPE_TXT, TYPE_ANY, TYPE_A, TYPE_AXFR = 16, 255, 1, 252

    @staticmethod
    def _static(path):
        return next(i for i in infra.MANIFEST["static_files"] if i.get("path") == path)

    def _config(self):
        return self._static("/opt/hams/etc/pdns-gsqlite3.conf")["content"]

    def _shipped_lua(self):
        """The script as provisioning writes it (static file contents go through str.format)."""
        return infra.format_env(self._static("/opt/hams/etc/pdns-prequery.lua")["content"], {})

    def _settings(self):
        return dict(l.split("=", 1) for l in self._config().splitlines() if "=" in l)

    def test_no_python_proxy_second_pdns_or_rate_limit_units_remain(self):
        paths = [i.get("path", "") for i in infra.MANIFEST["static_files"]]
        for gone in ("pdns-callbook.conf", "pdns.callbook.service", "callbook.dns.rrl.service"):
            self.assertFalse([p for p in paths if gone in p], gone)
        self.assertNotIn("callbook.dns.rrl.service", infra.SHARED_ODOO_ACCOUNT_UNITS)
        export = self._static("/opt/hams/systemd/callbook.dns.export.service")["content"]
        self.assertIn("After=network.target pdns.service", export)
        self.assertIn("CALLBOOK_PDNS_API_URL=http://127.0.0.1:8081/", export)
        self.assertNotIn("pdns.callbook", export)

    def test_one_instance_serves_both_zones_and_is_authoritative_only(self):
        c = self._settings()
        self.assertEqual(c["launch"], "gsqlite3:main,gsqlite3:callbook")
        self.assertEqual(c["gsqlite3-callbook-database"], "/var/lib/powerdns/callbook/callbook.sqlite3")
        self.assertNotIn("resolver", c)
        self.assertNotIn("recursor", c)
        self.assertNotIn("local-port", c, "port 53 is the default")
        self.assertNotEqual(c["expand-alias"] if "expand-alias" in c else "no", "yes")
        self.assertEqual((c["primary"], c["secondary"], c["disable-axfr"], c["allow-notify-from"]), ("no", "no", "yes", ""))

    def test_amplification_and_exposure_settings(self):
        c = self._settings()
        self.assertEqual(c["any-to-tcp"], "yes")
        # Bruce, 2026-10-04: PowerDNS's own default, so a ~700-byte callbook answer is ONE UDP packet.
        # The ~14x reflection factor for a forged 55-byte query is accepted (see the runbook).
        self.assertEqual(c["udp-truncation-threshold"], "1232")
        self.assertEqual(c["lua-prequery-script"], "/opt/hams/etc/pdns-prequery.lua")
        self.assertEqual(c["version-string"], "anonymous")
        self.assertEqual(c["security-poll-suffix"], "")
        self.assertEqual(c["log-dns-queries"], "no")
        self.assertLessEqual(int(c["max-tcp-connections-per-client"]), 10)
        self.assertGreater(int(c["max-tcp-transactions-per-conn"]), 0)
        self.assertEqual(c["webserver-address"], "127.0.0.1")
        self.assertEqual(c["webserver-allow-from"], "127.0.0.0/8,::1/128")
        self.assertEqual(c["allow-dnsupdate-from"], "127.0.0.0/8,::1/128")

    def test_callbook_database_is_created_before_pdns_starts(self):
        entry = next(d for d in infra.MANIFEST["directories"] if d["path"] == "/var/lib/powerdns/callbook")
        self.assertIn(infra.hook_create_callbook_sqlite_schema, entry["post_provision_hooks"])
        mock_run = MagicMock()
        self.safe_patch("infrastructure.apply_permissions")
        self.safe_patch("infrastructure._PDNS_SQLITE_SCHEMA_PATH", os.path.join(self.tmp, "none.sql"))
        infra.hook_create_callbook_sqlite_schema({}, "", self.tmp, mock_run)
        mock_run.assert_not_called()
        self.assertEqual(infra.get_hook_failures(), [], "a host without pdns needs no database")

    # ---- real pdns_server ----
    @staticmethod
    def _packet(name, qtype, bufsize=None, qclass=1):
        labels = b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split("."))
        extra = b"\x00\x00\x29" + struct.pack("!H", bufsize) + b"\x00\x00\x00\x00\x00\x00" if bufsize else b""
        header = struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 1 if bufsize else 0)
        return header + labels + b"\x00" + struct.pack("!HH", qtype, qclass) + extra

    @staticmethod
    def _reply_fields(data):
        flags = struct.unpack("!H", data[2:4])[0]
        return {"tc": bool(flags & 0x200), "ra": bool(flags & 0x80), "rcode": flags & 15,
                "answers": struct.unpack("!H", data[6:8])[0], "size": len(data)}

    def _udp(self, port, packet):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(3)
            sock.sendto(packet, ("127.0.0.1", port))
            return self._reply_fields(sock.recv(65535)) | {}

    def _tcp(self, port, packet):
        with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
            sock.sendall(struct.pack("!H", len(packet)) + packet)
            size = struct.unpack("!H", sock.recv(2))[0]
            data = b""
            while len(data) < size:
                chunk = sock.recv(size - len(data))
                if not chunk:
                    break
                data += chunk
            return self._reply_fields(data)

    def _make_db(self, path, zone, records):
        """records: (name relative to the zone or '', type, content). A SOA and an NS are added."""
        import sqlite3
        conn = sqlite3.connect(path)
        with open(self.SCHEMA) as fh:
            conn.executescript(fh.read())
        conn.execute("INSERT INTO domains(id, name, type) VALUES (1, ?, 'NATIVE')", (zone,))
        rows = [(1, zone, "SOA", f"ns1.{zone} hostmaster.{zone} 1 10800 3600 604800 3600", 300),
                (1, zone, "NS", f"ns1.{zone}", 300)]
        rows += [(1, f"{label}.{zone}" if label else zone, rtype, content, 300) for label, rtype, content in records]
        conn.executemany("INSERT INTO records(domain_id,name,type,content,ttl,auth) VALUES (?,?,?,?,?,1)",
                         rows)
        conn.commit()
        conn.close()

    def _udp_or_none(self, port, packet):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(1)
                sock.sendto(packet, ("127.0.0.1", port))
                return self._reply_fields(sock.recv(65535))
        except socket.timeout:
            return None

    @staticmethod
    def _txt(*strings):
        return " ".join('"' + x + '"' for x in strings)

    def _start_shipped_pdns(self):
        """A real pdns_server with exactly the shipped config and the exact shipped Lua script
        (only the file paths and the port differ). Returns the UDP/TCP port."""
        sockets = []
        for _ in range(2):
            s = socket.socket(); s.bind(("127.0.0.1", 0)); sockets.append(s)
        dns_port, web_port = (s.getsockname()[1] for s in sockets)
        for s in sockets:
            s.close()
        main_db = os.path.join(self.tmp, "main.sqlite3")
        callbook_db = os.path.join(self.tmp, "callbook.sqlite3")
        self._make_db(main_db, "u.example.org", [
            ("alias", "TXT", self._txt("small")), ("alias", "A", "192.0.2.7"), ("alias", "AAAA", "2001:db8::7"),
            ("alias", "MX", "10 mail.u.example.org"), ("alias", "CAA", '0 issue "letsencrypt.org"'),
            ("alias", "LOC", "37 52 0.000 N 122 16 0.000 W 0.00m 1m 10000m 10m"),
            ("_sip._tcp.alias", "SRV", "10 5 5060 alias.u.example.org"), ("www.alias", "CNAME", "alias.u.example.org"),
        ])
        self._make_db(callbook_db, "callbook.example.org", [
            ("k6bp", "TXT", self._txt("x" * 250, "y" * 250, "z" * 250)),
            ("huge", "TXT", self._txt(*["h" * 250] * 6)),
        ])
        config = self._config().replace("{DOMAIN}", "example.org").replace("{PDNS_API_KEY}", "testkey")
        config = config.replace("/var/lib/powerdns/pdns.sqlite3", main_db)
        config = config.replace("/var/lib/powerdns/callbook/callbook.sqlite3", callbook_db)
        shipped_lua = os.path.join(self.tmp, "pdns-prequery.lua")
        with open(shipped_lua, "w") as fh:
            fh.write(self._shipped_lua())
        self.assertIn("lua-prequery-script=/opt/hams/etc/pdns-prequery.lua", config)
        config = config.replace("/opt/hams/etc/pdns-prequery.lua", shipped_lua)
        config_dir = os.path.join(self.tmp, "etc")
        os.makedirs(config_dir)
        with open(os.path.join(config_dir, "pdns-shipped.conf"), "w") as fh:
            fh.write(config)
        sockdir = tempfile.mkdtemp(prefix="pdp")  # a UNIX socket path must be short
        self.addCleanup(lambda: shutil.rmtree(sockdir, ignore_errors=True))
        log = open(os.path.join(self.tmp, "pdns.log"), "w")
        proc = subprocess.Popen(
            ["pdns_server", "--guardian=no", "--daemon=no", "--disable-syslog", "--write-pid=no",
             f"--config-dir={config_dir}", "--config-name=shipped", f"--socket-dir={sockdir}",
             "--local-address=127.0.0.1", f"--local-port={dns_port}", f"--webserver-port={web_port}"],
            stdout=log, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: (proc.terminate(), proc.wait(10), log.close()))
        deadline = time.time() + 15
        while True:
            try:
                self._udp(dns_port, self._packet("alias.u.example.org", self.TYPE_TXT))
                return dns_port
            except OSError:
                if time.time() > deadline or proc.poll() is not None:
                    log.flush()
                    self.fail("pdns_server did not start: " + open(os.path.join(self.tmp, "pdns.log")).read()[-800:])
                time.sleep(0.2)

    def _skip_without_pdns(self):
        if not shutil.which("pdns_server") or not os.path.exists(self.SCHEMA):
            self.skipTest("pdns_server or its sqlite3 schema is not installed")

    def test_single_packet_answers_fit_at_1232_and_truncate_only_above_it(self):
        self._skip_without_pdns()
        port = self._start_shipped_pdns()
        small = self._udp(port, self._packet("alias.u.example.org", self.TYPE_TXT, bufsize=1232))
        self.assertEqual((small["rcode"], small["answers"], small["tc"]), (0, 1, False))
        # A callbook-sized answer (~800 bytes) goes out in ONE UDP packet to a client that advertises 1232.
        callbook = self._udp(port, self._packet("k6bp.callbook.example.org", self.TYPE_TXT, bufsize=1232))
        self.assertEqual((callbook["rcode"], callbook["answers"], callbook["tc"]), (0, 1, False))
        self.assertGreater(callbook["size"], 700)
        self.assertLessEqual(callbook["size"], 1232)
        # The same, and a bigger buffer, still fit; a client with no EDNS gets 512 bytes at most, so it is told to use TCP.
        self.assertFalse(self._udp(port, self._packet("k6bp.callbook.example.org", self.TYPE_TXT, bufsize=4096))["tc"])
        plain = self._udp(port, self._packet("k6bp.callbook.example.org", self.TYPE_TXT))
        self.assertTrue(plain["tc"] and plain["answers"] == 0 and plain["size"] <= 512, plain)
        # An answer above 1232 is never sent over UDP, whatever the client advertises.
        huge = self._udp(port, self._packet("huge.callbook.example.org", self.TYPE_TXT, bufsize=4096))
        self.assertTrue(huge["tc"] and huge["answers"] == 0 and huge["size"] <= 100, huge)
        over_tcp = self._tcp(port, self._packet("k6bp.callbook.example.org", self.TYPE_TXT))
        self.assertEqual((over_tcp["rcode"], over_tcp["answers"]), (0, 1))
        self.assertEqual(self._tcp(port, self._packet("huge.callbook.example.org", self.TYPE_TXT))["answers"], 1)

    def test_every_type_our_zones_hold_is_answered(self):
        self._skip_without_pdns()
        port = self._start_shipped_pdns()
        for qtype, name in ((1, "alias.u.example.org"), (28, "alias.u.example.org"), (15, "alias.u.example.org"),
                            (16, "alias.u.example.org"), (257, "alias.u.example.org"), (29, "alias.u.example.org"),
                            (33, "_sip._tcp.alias.u.example.org"), (5, "www.alias.u.example.org"),
                            (2, "u.example.org"), (6, "u.example.org"), (2, "callbook.example.org"),
                            (6, "callbook.example.org"), (16, "k6bp.callbook.example.org")):
            reply = self._udp(port, self._packet(name, qtype, bufsize=1232))
            self.assertEqual(reply["rcode"], 0, (name, qtype))
            self.assertGreaterEqual(reply["answers"], 1, (name, qtype))
        # Types we hold no record of, but that resolvers and browsers ask for any name, are an empty
        # NOERROR (NODATA), not REFUSED: refusing them would make resolvers distrust this server.
        for qtype in (43, 44, 35, 64, 65):
            reply = self._udp(port, self._packet("alias.u.example.org", qtype, bufsize=1232))
            self.assertEqual((reply["rcode"], reply["answers"]), (0, 0), qtype)
            self.assertLessEqual(reply["size"], 150, qtype)

    def test_unusual_query_types_are_refused_with_a_reply_no_larger_than_the_query(self):
        self._skip_without_pdns()
        port = self._start_shipped_pdns()
        # ANY, IXFR, AXFR, MAILB, MAILA, TKEY, TSIG, DNSKEY, RRSIG, NSEC, NSEC3, NULL, WKS, HINFO, ISDN, unassigned, 0.
        for qtype in (255, 251, 252, 253, 254, 249, 250, 48, 46, 47, 50, 10, 11, 13, 20, 99, 0, 65280):
            for name in ("alias.u.example.org", "k6bp.callbook.example.org", "u.example.org", "www.example.com"):
                query = self._packet(name, qtype, bufsize=4096)
                reply = self._udp(port, query)
                self.assertEqual(reply["rcode"], 5, (name, qtype, reply))
                self.assertEqual(reply["answers"], 0)
                self.assertLessEqual(reply["size"], len(query), (name, qtype, "no amplification"))
        # Measured: the prequery hook runs for UDP only. Over TCP (the handshake proves the source address)
        # ANY is answered normally, which is what RFC 8482 allows and what we want.
        any_tcp = self._tcp(port, self._packet("k6bp.callbook.example.org", self.TYPE_ANY))
        self.assertEqual((any_tcp["rcode"], any_tcp["answers"]), (0, 1), any_tcp)

    def test_what_powerdns_itself_does_with_the_rest(self):
        """The Lua hook cannot see class, opcode or flags, so these are PowerDNS's own behavior,
        pinned here so an upgrade that changes it fails a test."""
        self._skip_without_pdns()
        port = self._start_shipped_pdns()
        for qclass in (3, 4):  # CH, HS: refused or not implemented, never an answer, never larger than the query
            query = self._packet("alias.u.example.org", self.TYPE_TXT, qclass=qclass, bufsize=1232)
            reply = self._udp(port, query)
            self.assertIn(reply["rcode"], (4, 5), qclass)
            self.assertEqual(reply["answers"], 0)
            self.assertLessEqual(reply["size"], len(query))
        # Class ANY (255) is answered like IN, with an answer of the same size: no amplification beyond IN.
        as_in = self._udp(port, self._packet("alias.u.example.org", self.TYPE_TXT, bufsize=1232))
        as_any = self._udp(port, self._packet("alias.u.example.org", self.TYPE_TXT, qclass=255, bufsize=1232))
        self.assertEqual((as_any["rcode"], as_any["size"]), (as_in["rcode"], as_in["size"]))
        # Opcodes IQUERY, STATUS and 3 and a reply sent as a query are dropped without any answer; NOTIFY and
        # UPDATE are answered with an error (FORMERR/NOTIMP/REFUSED) no larger than the query.
        for opcode in (1, 2, 3):
            self.assertIsNone(self._udp_or_none(port, self._query_with(opcode=opcode)), opcode)
        for opcode in (4, 5):
            reply = self._udp_or_none(port, self._query_with(opcode=opcode))
            self.assertIn(reply["rcode"], (1, 4, 5), opcode)
            self.assertEqual(reply["answers"], 0)
            self.assertLessEqual(reply["size"], len(self._query_with(opcode=opcode)))
        self.assertIsNone(self._udp_or_none(port, self._query_with(flags=0x8100)), "a packet with the response bit set")
        # Malformed packets are dropped by PowerDNS itself: a truncated header, a question count with no question.
        self.assertIsNone(self._udp_or_none(port, b"\x12\x34\x01"))
        self.assertIsNone(self._udp_or_none(port, b"\x12\x34\x01\x00\x00\x05\x00\x00\x00\x00\x00\x00"))

    def _query_with(self, opcode=0, flags=0x0100):
        packet = bytearray(self._packet("alias.u.example.org", self.TYPE_TXT))
        struct.pack_into("!H", packet, 2, flags | (opcode << 11))
        return bytes(packet)

    def test_names_outside_our_zones_are_still_refused_and_it_is_not_a_resolver(self):
        self._skip_without_pdns()
        port = self._start_shipped_pdns()
        for qtype in (self.TYPE_A, self.TYPE_TXT, 28, 2, 6):
            query = self._packet("www.example.com", qtype, bufsize=1232)
            outside = self._udp(port, query)
            self.assertEqual(outside["rcode"], 5, qtype)
            self.assertFalse(outside["ra"], "recursion is not available")
            self.assertEqual(outside["answers"], 0)
            self.assertLessEqual(outside["size"], len(query))
        self.assertEqual(self._tcp(port, self._packet("www.example.com", self.TYPE_A))["rcode"], 5)
        self.assertEqual(self._udp(port, self._packet(".", 2))["rcode"], 5)
        axfr = self._tcp(port, self._packet("callbook.example.org", self.TYPE_AXFR))
        self.assertTrue(axfr["rcode"] != 0 or axfr["answers"] == 0, axfr)
        version = self._udp(port, self._packet("version.bind", self.TYPE_TXT, qclass=3))
        self.assertEqual(version["answers"], 0, "no version string is revealed")

    def test_the_shipped_script_fails_open_and_uses_only_the_two_documented_calls(self):
        script = self._shipped_lua()
        self.assertNotIn("{{", script, "provisioning's str.format turns doubled braces into one; none may remain")
        self.assertIn("pcall(", script, "a changed PowerDNS API must not SERVFAIL every query")
        self.assertEqual(sorted(set(re.findall(r"\bp:(\w+)\(", script))), ["getQuestion", "setRcode"])
        served = {int(n) for n in re.findall(r"\[(\d+)\] = true", script)}
        self.assertEqual(served, {1, 2, 5, 6, 15, 16, 28, 29, 33, 35, 43, 44, 64, 65, 257})
        for never in (255, 251, 252, 48, 46, 47):
            self.assertNotIn(never, served)


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
        # Two databases: the main one, and callbook.sqlite3 (the second backend of the one
        # PowerDNS that owns port 53, which needs its tables before it starts).
        self.assertEqual(len(sqlite_calls), 2)
        self.assertEqual(
            sorted(c.args[0][1] for c in sqlite_calls),
            sorted(os.path.join(self.tmp, "var/lib/powerdns", n) for n in ("pdns.sqlite3", "callbook/callbook.sqlite3")),
        )
        for call in sqlite_calls:
            self.assertIn("stdin", call.kwargs)

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
        # Tests [@ANCHOR: infrastructure:hook_daemons_perms]
        # Only root-owned entries change owner, so odoo-owned daemon paths keep their owner.
        self.assertEqual(
            mock_run.call_args_list[0][0][0],
            ["find", "-P", target, "-user", "root", "-exec", "chown", "-h", "hams_com:hams_com", "{}", "+"],
        )
        self.assertEqual(mock_run.call_args_list[1][0][0], ["chmod", "-R", "a+rX", target])

    def test_the_find_selector_leaves_non_root_entries_alone(self):
        # Run the real find (with `-print` in place of the chown) over a tree holding a file owned
        # by this non-root test user: it must not be selected.
        target = os.path.join(self.tmp, "daemons")
        os.makedirs(target)
        mine = os.path.join(target, "owned_by_someone_else.py")
        open(mine, "w").close()
        commands = []
        infra.hook_daemons_perms({}, "", target, commands.append)
        find_cmd = commands[0]
        selector = find_cmd[: find_cmd.index("-exec")] + ["-print"]
        out = subprocess.run(selector, capture_output=True, text=True, check=True).stdout.split()
        if os.geteuid() != 0:
            self.assertNotIn(mine, out)
        self.assertNotIn("-R", find_cmd)

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

    def test_pdns_zone_defaults_derive_from_domain(self):
        # [@ANCHOR: infrastructure:test_pdns_zone_defaults]
        # Tests [@ANCHOR: infrastructure:set_pdns_zone_defaults]
        # pdns_sync names these in every zone it creates; Cloudflare
        # delegates u.<DOMAIN> to ns1/ns2.<DOMAIN> (hams_com, 2026-10-03).
        env_vars = {"DOMAIN": "hams.com"}
        infra.load_and_prompt_env(env_vars, is_test=False)
        self.assertEqual(
            env_vars["PDNS_ZONE_NAMESERVERS"], "ns1.hams.com,ns2.hams.com"
        )
        self.assertEqual(env_vars["PDNS_PERSONAL_PARENT_ZONE"], "u.hams.com")
        self.assertIn(
            "PDNS_ZONE_NAMESERVERS", infra.MANIFEST["env_groups"]["pdns.env"]
        )
        self.assertIn(
            "PDNS_PERSONAL_PARENT_ZONE",
            infra.MANIFEST["env_groups"]["pdns.env"],
        )

        test_vars = {}
        infra.load_and_prompt_env(test_vars, is_test=True)
        self.assertEqual(test_vars["PDNS_PERSONAL_PARENT_ZONE"], "u.localhost")

        kept = {"DOMAIN": "hams.com", "PDNS_ZONE_NAMESERVERS": "a.example."}
        infra.load_and_prompt_env(kept, is_test=False)
        self.assertEqual(kept["PDNS_ZONE_NAMESERVERS"], "a.example.")

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


class BridgeApiKeyProvisioningTests(_TmpDirTestCase):
    """BRIDGE_API_KEY (bridge.env) and Odoo's ham_relay_bridge.api_key must exist and agree after
    provisioning. hams1 ran 2026-09-23..10-03 with both empty, and every relay uplink was refused.
    subprocess.run is mocked: these never reach a real database, and no assertion prints a key."""

    KEY = "k" * 64

    def setUp(self):
        super().setUp()
        self.safe_patch("infrastructure.os.path.exists", return_value=False)

    def _psql(self, matches_sequence, db_exists=True):
        """Mocks the read-only probes: _database_exists, then each _bridge_api_key_matches_odoo."""
        self.safe_patch("infrastructure._database_exists", return_value=db_exists)
        results = [
            subprocess.CompletedProcess([], 0, stdout="1\n" if m else "0\n", stderr="")
            for m in matches_sequence
        ]
        return self.safe_patch("infrastructure.subprocess.run", side_effect=results)

    def test_generates_a_key_in_both_modes_when_absent_or_empty(self):
        for is_test, start in ((False, {"DOMAIN": "hams.com"}), (True, {}),
                               (False, {"DOMAIN": "hams.com", "BRIDGE_API_KEY": ""})):
            env_vars = dict(start)
            infra.load_and_prompt_env(env_vars, is_test=is_test)
            self.assertGreaterEqual(len(env_vars["BRIDGE_API_KEY"]), 64)

    def test_keeps_an_existing_key(self):
        env_vars = {"DOMAIN": "hams.com", "BRIDGE_API_KEY": "already-set"}
        infra.load_and_prompt_env(env_vars, is_test=False)
        self.assertEqual(env_vars["BRIDGE_API_KEY"], "already-set")

    def test_write_env_files_persists_it_to_bridge_env_at_0400(self):
        run = MagicMock()
        self.safe_patch("infrastructure.apply_permissions")
        infra.write_env_files(self.tmp, {"BRIDGE_API_KEY": self.KEY}, run)
        path = os.path.join(self.tmp, "bridge.env")
        self.assertEqual(infra._read_env_value(path, "BRIDGE_API_KEY"), self.KEY)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o400)

    def test_matching_parameter_is_left_alone(self):
        self._psql([True])
        run = MagicMock()
        infra._sync_bridge_api_key_to_odoo(run, "hams_prod", self.KEY)
        run.assert_not_called()
        self.assertEqual(infra.get_hook_failures(), [])

    def test_missing_or_different_parameter_is_written_then_verified(self):
        # Tests [@ANCHOR: infrastructure:_sync_bridge_api_key_to_odoo]
        # Tests [@ANCHOR: infrastructure:_bridge_api_key_matches_odoo]
        probe = self._psql([False, True])
        run = MagicMock()
        infra._sync_bridge_api_key_to_odoo(run, "hams_prod", self.KEY)
        run.assert_called_once()
        cmd = run.call_args.args[0]
        sql = run.call_args.kwargs["input"]
        self.assertIn(f"bridge_api_key={self.KEY}", cmd)
        self.assertIn("ham_relay_bridge.api_key", sql)
        self.assertIn("ON CONFLICT (key) DO UPDATE", sql)
        # The key travels only as a psql variable, never inside the SQL text.
        self.assertNotIn(self.KEY, sql)
        for call in probe.call_args_list:
            self.assertNotIn(self.KEY, call.kwargs["input"])
        self.assertEqual(infra.get_hook_failures(), [])

    def test_a_value_that_does_not_read_back_is_a_hook_failure(self):
        self._psql([False, False])
        infra._sync_bridge_api_key_to_odoo(MagicMock(), "hams_prod", self.KEY)
        [(name, msg)] = infra.get_hook_failures()
        self.assertEqual(name, "bridge_api_key")
        self.assertNotIn(self.KEY, msg)

    def test_a_failed_write_records_no_key(self):
        self._psql([False])
        run = MagicMock(side_effect=subprocess.CalledProcessError(1, ["psql", f"bridge_api_key={self.KEY}"]))
        infra._sync_bridge_api_key_to_odoo(run, "hams_prod", self.KEY)
        [(name, msg)] = infra.get_hook_failures()
        self.assertEqual(name, "bridge_api_key")
        self.assertNotIn(self.KEY, msg)

    def test_an_empty_key_is_a_hook_failure(self):
        run = MagicMock()
        infra._sync_bridge_api_key_to_odoo(run, "hams_prod", "")
        run.assert_not_called()
        self.assertEqual([n for n, _ in infra.get_hook_failures()], ["bridge_api_key"])

    def test_plan_mode_probes_but_never_writes(self):
        self._psql([False])
        run = MagicMock()
        with infra.planning() as plan:
            infra._sync_bridge_api_key_to_odoo(run, "hams_prod", self.KEY)
        run.assert_not_called()
        self.assertEqual(len(plan.actions), 1)
        self.assertIn("ham_relay_bridge.api_key", plan.actions[0][1])
        self.assertNotIn(self.KEY, plan.actions[0][1])

    def test_redact_command_masks_the_key_variable(self):
        printed = " ".join(infra.redact_command(["psql", "-v", f"bridge_api_key={self.KEY}"]))
        self.assertNotIn(self.KEY, printed)
        self.assertIn("bridge_api_key=<redacted>", printed)


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

        # The role exists but no env file records its password: rotate it.
        os.remove(self._env_path())
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

    def test_a_rerun_keeps_the_recorded_password_and_alters_nothing(self):
        # Round-2 production runbook, 2026-10-03: the password used to rotate on every run.
        run_cmd = MagicMock()
        self.safe_patch_object(infra, "_role_exists", return_value=False)
        self.safe_patch_object(infra, "apply_permissions")
        env_path = self._env_path()

        infra._provision_cache_manager_role(run_cmd, "hams_test", env_file=env_path)
        with open(env_path) as f:
            first = f.read()
        mtime = os.stat(env_path).st_mtime_ns

        run_cmd.reset_mock()
        self.safe_patch_object(infra, "_role_exists", return_value=True)
        infra._provision_cache_manager_role(run_cmd, "hams_test", env_file=env_path)
        with open(env_path) as f:
            self.assertEqual(f.read(), first)
        self.assertEqual(os.stat(env_path).st_mtime_ns, mtime)
        sql = [c.kwargs["input"] for c in run_cmd.call_args_list]
        self.assertFalse(any("ROLE" in q and "PASSWORD" in q for q in sql), sql)
        self.assertTrue(any("GRANT CONNECT" in q for q in sql))

    def test_a_missing_role_is_created_with_the_recorded_password(self):
        # Tests [@ANCHOR: infrastructure:_read_env_value]
        run_cmd = MagicMock()
        self.safe_patch_object(infra, "apply_permissions")
        env_path = self._env_path()
        os.makedirs(os.path.dirname(env_path))
        with open(env_path, "w") as f:
            f.write("DB_PASS=kept-secret-value\n")
        self.safe_patch_object(infra, "_role_exists", return_value=False)
        infra._provision_cache_manager_role(run_cmd, "hams_test", env_file=env_path)
        first = run_cmd.call_args_list[0]
        self.assertIn("CREATE ROLE", first.kwargs["input"])
        self.assertIn("db_pass=kept-secret-value", first.args[0])
        self.assertEqual(infra._read_env_value(env_path, "DB_PASS"), "kept-secret-value")


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
        limits = dict(infra.ODOO_CONF_GEVENT_MEMORY_LIMITS + infra.ODOO_CONF_WORKER_LIMITS)
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
        # a longer key sharing its prefix (limit_memory_soft must not take out
        # limit_memory_soft_gevent, nor limit_time_real take out limit_time_real_cron).
        conf_lines = [
            "limit_memory_soft = 1", "limit_memory_hard = 4294967296",
            "limit_memory_soft_gevent = 1", "limit_memory_hard_gevent = 1",
            "limit_memory_soft_gevent_x = 1",
            "limit_time_real = 1", "limit_time_real_cron = 1", "limit_time_cpu = 1", "max_cron_threads = 1",
        ]
        for c in cmds:
            if c[:3] != ["sudo", "sed", "-i"] or not ("limit_" in c[3] or "max_cron" in c[3]):
                continue
            address = c[3][1:-2].replace("[[:space:]]", r"\s")  # strip "/" ... "/d"
            matched = [line for line in conf_lines if re.match(address, line)]
            self.assertEqual(len(matched), 1, f"{c[3]} must delete exactly one key, got {matched}")
            self.assertIn(matched[0].split(" = ")[0], limits)


class OdooLogrotateTests(unittest.TestCase):
    """Audit row 19: Odoo's log is rotated daily, compressed and kept for two weeks."""

    def _entry(self):
        return next(s for s in infra.MANIFEST["static_files"] if s["path"] == "/etc/logrotate.d/odoo")

    def test_rendered_file_has_the_audited_directives(self):
        text = infra.format_env(self._entry()["content"], {})
        self.assertTrue(text.startswith("/var/log/odoo/*.log {\n"), text)
        directives = {line.strip() for line in text.splitlines()}
        for wanted in ("daily", "rotate 14", "maxsize 300M", "compress", "delaycompress", "copytruncate",
                       "missingok", "notifempty"):
            self.assertIn(wanted, directives)
        self.assertNotIn("weekly", directives)
        self.assertEqual(text.count("{"), 1)
        self.assertEqual(text.count("}"), 1)

    def test_it_is_for_production_and_root_owned(self):
        entry = self._entry()
        self.assertEqual(entry["environments"], ["prod"])
        self.assertEqual((entry["owner"], entry["mode"]), ("root:root", "644"))

    def test_the_path_matches_the_log_odoo_writes(self):
        self.assertIn("/var/log/odoo/", self._entry()["content"])


class OdooWorkerLimitsTests(unittest.TestCase):
    """Tests [@ANCHOR: infrastructure:odoo_conf_gevent_memory_limits] for the worker pool's explicit limits (audit row 16)."""

    def test_the_limits_are_the_audited_values(self):
        limits = dict(infra.ODOO_CONF_WORKER_LIMITS)
        self.assertEqual(limits["limit_memory_soft"], 2 * 1024 ** 3)
        self.assertEqual(limits["limit_memory_hard"], 4 * 1024 ** 3)
        self.assertEqual((limits["limit_time_cpu"], limits["limit_time_real"], limits["limit_time_real_cron"]),
                         (120, 240, 1800))
        self.assertEqual(limits["max_cron_threads"], 2)

    def test_soft_is_below_hard_and_a_request_cannot_outlast_a_cron_job(self):
        limits = dict(infra.ODOO_CONF_WORKER_LIMITS)
        self.assertLess(limits["limit_memory_soft"], limits["limit_memory_hard"])
        self.assertLess(limits["limit_time_cpu"], limits["limit_time_real"])
        self.assertLessEqual(limits["limit_time_real"], limits["limit_time_real_cron"])

    def test_workers_is_not_changed_by_this(self):
        self.assertNotIn("workers", dict(infra.ODOO_CONF_WORKER_LIMITS))

    def test_the_limits_are_in_the_provisioning_loop(self):
        source = inspect.getsource(infra.initialize_odoo_database)
        self.assertIn("ODOO_CONF_GEVENT_MEMORY_LIMITS + ODOO_CONF_WORKER_LIMITS", source)


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
        elif_idx = source.index("elif not is_isolated_ns", hold_idx)
        init_idx = source.index("initialize_odoo_database(\n", hold_idx)
        smoke_idx = source.index("run_post_provision_smoketest(\n", hold_idx)
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
        self.assertIn('"/etc/rabbitmq/rabbitmq-env.conf", "NODE_IP_ADDRESS=127.0.0.1"', source)
        self.assertIn("rabbitmq_conf_changed = _ensure_line_in_file(", source)
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
        source = inspect.getsource(infra._apply_postgresql_lockdown).split('"""')[-1]
        self.assertNotIn("pg_hba", source)
        self.assertNotIn("trust", source)
        for key, value in infra.POSTGRESQL_LOCKDOWN_SETTINGS:
            self.assertNotIn("pg_hba", key + value)

    def test_still_binds_postgresql_to_loopback(self):
        # Tests [@ANCHOR: infrastructure:postgresql_lockdown]
        settings = dict(infra.POSTGRESQL_LOCKDOWN_SETTINGS)
        self.assertEqual(settings["listen_addresses"], "'127.0.0.1, ::1'")
        self.assertEqual(settings["shared_preload_libraries"], "'pg_stat_statements'")

    def _conf(self, text):
        path = os.path.join(self._tmpdir.name, "postgresql.conf")
        with open(path, "w") as f:
            f.write(text)
        return path

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    def test_collapses_duplicate_pairs_without_asking_for_a_restart(self):
        # Tests [@ANCHOR: infrastructure:_apply_postgresql_lockdown]
        # hams1 had eleven duplicate pairs, all with the wanted values.
        pair = "listen_addresses = '127.0.0.1, ::1'\nshared_preload_libraries = 'pg_stat_statements'\n"
        path = self._conf("#listen_addresses = 'localhost'\nport = 5432\n" + pair * 11)
        self.assertFalse(infra._apply_postgresql_lockdown([path]))
        with open(path) as f:
            text = f.read()
        self.assertEqual(text, "#listen_addresses = 'localhost'\nport = 5432\n" + pair)
        # And a second run changes nothing at all.
        self.assertFalse(infra._apply_postgresql_lockdown([path]))
        with open(path) as f:
            self.assertEqual(f.read(), text)

    def test_a_real_value_change_asks_for_a_restart(self):
        path = self._conf("listen_addresses = '*'\nport = 5432")
        self.assertTrue(infra._apply_postgresql_lockdown([path]))
        with open(path) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0], "listen_addresses = '127.0.0.1, ::1'")
        self.assertIn("shared_preload_libraries = 'pg_stat_statements'", lines)
        self.assertEqual(sum(1 for line in lines if line.startswith("listen_addresses")), 1)

    def test_set_conf_key_uses_the_last_active_line_as_the_effective_value(self):
        # Tests [@ANCHOR: infrastructure:_set_conf_key]
        text = "listen_addresses = '127.0.0.1, ::1'  # ok\nlisten_addresses = '*'\n"
        new_text, changed = infra._set_conf_key(text, "listen_addresses", "'127.0.0.1, ::1'")
        self.assertTrue(changed)
        self.assertEqual(new_text, "listen_addresses = '127.0.0.1, ::1'\n")
        _, changed = infra._set_conf_key(new_text, "listen_addresses", "'127.0.0.1, ::1'")
        self.assertFalse(changed)
        # Commented-out lines are neither counted nor removed.
        new_text, changed = infra._set_conf_key("#listen_addresses = 'x'\n", "listen_addresses", "'y'")
        self.assertTrue(changed)
        self.assertEqual(new_text, "#listen_addresses = 'x'\nlisten_addresses = 'y'\n")

    def test_plan_mode_writes_nothing(self):
        path = self._conf("listen_addresses = '*'\n")
        with infra.planning() as plan:
            self.assertTrue(infra._apply_postgresql_lockdown([path]))
        with open(path) as f:
            self.assertEqual(f.read(), "listen_addresses = '*'\n")
        self.assertEqual(plan.actions[0][0], "write")

    def test_provision_environment_uses_the_lockdown_commands_and_no_pg_hba_edit(self):
        # provision_environment() itself is too host-dependent to execute
        # here (see this file's docstring), so check its source: the
        # blanket substitution must not come back inline.
        source = inspect.getsource(infra.provision_environment)
        self.assertIn("_apply_postgresql_lockdown()", source)
        # The restart is conditional on a real change (a PostgreSQL restart can crash odoo).
        restart_idx = source.index('["systemctl", "restart", "postgresql"]')
        self.assertIn("if postgresql_restart_needed:", source[restart_idx - 200:restart_idx])
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

    hams_com's relay_signer/subcarrier_signer daemons
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


class SignerDaemonManifestTests(unittest.TestCase):
    """The MANIFEST pieces hams_com's three signer daemons need to start at
    all. The defaults below are copied from each daemon's own main.py
    (BASE_DIR/PUBLIC_DIR/SOCKET_PATH) and its Odoo-side client."""

    SIGNERS = {
        "subcarrier_signer": ("subcarrier-signer", "SUBCARRIER_SIGNER"),
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
            "subcarrier_signer": [infra.hook_migrate_subcarrier_signing_key],
            "relay_signer": None,
        }
        for name in self.SIGNERS:
            owner = f"hams_{name}:hams_{name}"
            private = dirs[f"/opt/hams/etc/{name}"]
            public = dirs[f"/opt/hams/etc/{name}_public"]
            self.assertEqual((private["owner"], private["provision_mode"]), (owner, "700"))
            self.assertEqual((public["owner"], public["provision_mode"]), (owner, "755"))
            if hooks[name] is None:
                self.assertNotIn("post_provision_hooks", private)
            else:
                self.assertEqual(private["post_provision_hooks"], hooks[name])

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



class RelayCaSignerManifestTests(unittest.TestCase):
    """The MANIFEST pieces for hams_com's daemons/relay_ca: THREE signer daemons on hams1 (docs/proposals/
    CLOUD_HSM_CA_SIGNING.md), each its own account, state directory, Unix socket and unit, each holding its own Google
    service-account key file and signing with one Google Cloud KMS HSM key. They belong to the "ca_signer" host class
    and to prod only, so no other host (the dev box, every test host) gets an account, a directory or a unit, and no
    other host holds a signing credential. The defaults are copied from daemons/relay_ca/signer_daemon.py."""

    SIGNERS = {
        # purpose: (unit, account, state dir, public dir or None, allowed socket users, odoo joins the group)
        "relay": ("hams-relay-ca.service", "hams_relay_ca", "relay_ca", "relay_ca_public", "odoo", True),
        "identity": ("hams-identity-ca.service", "hams_identity_ca", "identity_ca", "identity_ca_public", "odoo", True),
        "capability": ("hams-capability-ca.service", "hams_capability_ca", "capability_ca", None, "root", False),
    }

    def _spec(self, unit):
        for entry in infra.MANIFEST["static_files"]:
            if entry["path"] == f"/opt/hams/systemd/{unit}":
                return entry
        self.fail(f"no {unit} in static_files")

    def test_each_signer_has_its_own_account_and_only_odoo_joins_the_ones_it_may_ask(self):
        accounts = {a["user"]: a for a in infra.MANIFEST["system_accounts"]}
        for purpose, (unit, account, state, public, allowed, odoo_joins) in self.SIGNERS.items():
            spec = accounts[account]
            self.assertEqual((spec["group"], spec["home"], spec["shell"]),
                             (account, f"/opt/hams/etc/{state}", "/usr/sbin/nologin"), purpose)
            self.assertEqual((spec["host_class"], spec["environments"]), ("ca_signer", ["prod"]), purpose)
            self.assertEqual(spec.get("add_to_users"), ["odoo"] if odoo_joins else None, purpose)
        self.assertEqual(len({a["user"] for a in accounts.values() if a["user"] in {v[1] for v in self.SIGNERS.values()}}), 3)

    def test_private_dirs_are_0700_public_dirs_0755_and_all_are_class_gated_and_prod_only(self):
        dirs = {d["path"]: d for d in infra.MANIFEST["directories"]}
        for purpose, (unit, account, state, public, allowed, _) in self.SIGNERS.items():
            private = dirs[f"/opt/hams/etc/{state}"]
            self.assertEqual((private["owner"], private["provision_mode"]), (f"{account}:{account}", "700"), purpose)
            self.assertNotIn("post_provision_hooks", private)
            for entry in (private, dirs[f"/opt/hams/etc/{public}"] if public else private):
                self.assertEqual((entry["host_class"], entry["environments"]), ("ca_signer", ["prod"]), purpose)
            if public:
                self.assertEqual(dirs[f"/opt/hams/etc/{public}"]["provision_mode"], "755")
        self.assertNotIn("/opt/hams/etc/capability_ca_public", dirs)

    def test_nothing_naming_a_signer_account_or_directory_exists_on_a_host_without_the_class(self):
        pattern = re.compile(r"hams_(relay|identity|capability)_ca|/opt/hams/etc/(relay|identity|capability)_ca(?!_client)")
        for kind in ("static_files", "directories", "system_accounts"):
            for spec in infra.MANIFEST[kind]:
                text = json.dumps(spec, default=str)
                if pattern.search(text):
                    self.assertEqual(spec.get("host_class"), "ca_signer", spec.get("path") or spec.get("user"))
                    self.assertEqual(spec.get("environments"), ["prod"], spec.get("path") or spec.get("user"))

    def test_the_odoo_side_directory_holds_only_the_pinned_certificate_and_no_credential(self):
        dirs = {d["path"]: d for d in infra.MANIFEST["directories"]}
        client = dirs["/opt/hams/etc/relay_ca_client"]
        self.assertEqual((client["owner"], client["provision_mode"]), ("odoo:odoo", "700"))
        self.assertNotIn("host_class", client)

    def test_each_unit_runs_isolated_talks_only_to_google_and_its_socket_and_waits_for_setup(self):
        for purpose, (unit_name, account, state, public, allowed, _) in self.SIGNERS.items():
            spec = self._spec(unit_name)
            unit = spec["content"]
            self.assertTrue(unit.startswith("[Unit]\n"), purpose)
            # Skipped, not restart-looping, until cloudkms_setup.py has written the key file and signer.env.
            self.assertIn(f"ConditionPathExists=/opt/hams/etc/{state}/signer.env\n", unit)
            self.assertIn(f"ConditionPathExists=/opt/hams/etc/{state}/gcp_service_account.json\n", unit)
            self.assertIn(f"User={account}\nGroup={account}\n", unit)
            self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n", unit)
            self.assertIn("NoNewPrivileges=true\n", unit)
            self.assertIn("CapabilityBoundingSet=\n", unit)
            self.assertIn(f"ReadWritePaths=/opt/hams/etc/{state}\n", unit)
            self.assertIn(f"RuntimeDirectory={account}\nRuntimeDirectoryMode=0750\n", unit)
            for key, value in (
                ("PURPOSE", purpose), ("STATE_DIR", f"/opt/hams/etc/{state}"),
                ("SOCKET_PATH", f"/run/{account}/signer.sock"),
                ("CREDENTIALS_FILE", f"/opt/hams/etc/{state}/gcp_service_account.json"), ("ALLOWED_USERS", allowed),
            ):
                self.assertIn(f'Environment="RELAY_CA_{key}={value}"\n', unit, purpose)
            if public:
                self.assertIn(f'Environment="RELAY_CA_PUBLIC_DIR=/opt/hams/etc/{public}"\n', unit)
            self.assertIn(f"EnvironmentFile=/opt/hams/etc/{state}/signer.env\n", unit)
            main_py = "/opt/hams/daemons/relay_ca/signer_daemon.py"
            self.assertIn(f"ExecStartPre=/usr/bin/python3 {main_py} --start-test\n", unit)
            self.assertIn(f"ExecStart=/usr/bin/python3 {main_py}\n", unit)
            self.assertIn("WantedBy=multi-user.target\n", unit)
            self.assertNotIn("IPAddressAllow", unit)
            self.assertNotIn("WireGuard", unit)
            self.assertEqual((spec["host_class"], spec["environments"]), ("ca_signer", ["prod"]), purpose)

    def test_the_capability_root_answers_root_alone_and_reads_its_issuer_allowlist_from_its_own_directory(self):
        unit = self._spec("hams-capability-ca.service")["content"]
        self.assertIn('Environment="RELAY_CA_ALLOWED_USERS=root"\n', unit)
        self.assertIn('RELAY_CA_CAPABILITY_ISSUERS_FILE=/opt/hams/etc/capability_ca/capability_issuers.json', unit)
        self.assertNotIn("RELAY_CA_PUBLIC_DIR", unit)

    def test_the_signers_call_google_so_they_are_external_fetch_and_opt_in_and_never_started_by_provisioning(self):
        names = {v[0] for v in self.SIGNERS.values()}
        self.assertEqual(names & infra.external_fetch_unit_names(), names)
        self.assertEqual(names & infra.opt_in_unit_names(), names)
        with patch.object(infra, "host_classes", return_value={"ca_signer"}):
            self.assertFalse(names & set(infra._smoketest_candidate_services()))
        for name in names:
            self.assertFalse(name in infra._activation_units_to_enable([name], is_test_env=False))

    def test_no_signer_unit_for_the_retired_pi500_host_or_for_wireguard_remains(self):
        paths = [e["path"] for e in infra.MANIFEST["static_files"]]
        self.assertNotIn("/opt/hams/systemd/hams-relay-ca-signer.service", paths)
        text = json.dumps(infra.MANIFEST, default=str)
        self.assertNotIn("RELAY_CA_BIND", text)
        self.assertNotIn("authorized_clients", text)

    def test_the_kms_client_library_is_provisioned(self):
        with open(infra.__file__, encoding="utf-8") as f:
            self.assertIn('"google-cloud-kms"', f.read())


class HostClassTests(unittest.TestCase):
    """Tests [@ANCHOR: infrastructure:host_classes]"""

    def test_plain_host_has_no_class_and_skips_class_specs(self):
        with tempfile.TemporaryDirectory() as tmp:
            classes = infra.host_classes(environ={}, path=os.path.join(tmp, "host_classes"))
        self.assertEqual(classes, set())
        self.assertTrue(infra._in_host_class({"path": "/x"}, classes))
        self.assertFalse(infra._in_host_class({"path": "/x", "host_class": "ca_signer"}, classes))
        self.assertTrue(infra._in_host_class({"path": "/x", "host_class": "ca_signer"}, {"ca_signer"}))

    def test_classes_come_from_the_environment_and_the_file_and_typos_are_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "host_classes")
            self.assertTrue(infra.record_host_class("ca_signer", path))
            self.assertFalse(infra.record_host_class("ca_signer", path), "idempotent")
            self.assertEqual(infra.host_classes(environ={}, path=path), {"ca_signer"})
            self.assertEqual(infra.host_classes(environ={"HAMS_HOST_CLASSES": "ca_signer"}, path=path + "x"),
                             {"ca_signer"})
            with open(path, "a") as f:
                f.write("# comment\nca_signr\n")
            with self.assertRaises(ValueError):
                infra.host_classes(environ={}, path=path)
            with self.assertRaises(ValueError):
                infra.record_host_class("ca_signr", path)

    def test_a_plain_host_creates_no_signer_account_directory_file_or_unit(self):
        calls = []
        with patch.object(infra, "host_classes", return_value=set()), \
                patch.object(infra, "grp") as grp, patch.object(infra, "pwd") as pwd:
            grp.getgrnam.side_effect = KeyError
            pwd.getpwnam.side_effect = KeyError
            infra.provision_system_accounts(lambda cmd, **kw: calls.append(cmd), environment="prod")
        created = " ".join(" ".join(c) for c in calls)
        for account in ("hams_relay_ca", "hams_identity_ca", "hams_capability_ca"):
            self.assertNotIn(account, created)
        self.assertIn("hams_relay_signer", created, "the other signers are still created")
        with patch.object(infra, "host_classes", return_value={"ca_signer"}), \
                patch.object(infra, "grp") as grp, patch.object(infra, "pwd") as pwd:
            grp.getgrnam.side_effect = KeyError
            pwd.getpwnam.side_effect = KeyError
            calls.clear()
            infra.provision_system_accounts(lambda cmd, **kw: calls.append(cmd), environment="prod")
        joined = " ".join(" ".join(c) for c in calls)
        for account in ("hams_relay_ca", "hams_identity_ca", "hams_capability_ca"):
            self.assertIn(account, joined)
        with patch.object(infra, "host_classes", return_value=set()):
            self.assertNotIn("/opt/hams/etc/relay_ca", infra.get_mount_paths("prod", "ro"))
            self.assertIn("/opt/hams/etc/relay_ca_client", infra.get_mount_paths("prod", "ro"))
        with patch.object(infra, "host_classes", return_value={"ca_signer"}):
            self.assertIn("/opt/hams/etc/relay_ca", infra.get_mount_paths("prod", "ro"))
            self.assertIn("/opt/hams/etc/identity_ca", infra.get_mount_paths("prod", "ro"))

    def test_only_host_class_plan_touches_only_that_class(self):
        # Tests [@ANCHOR: infrastructure:provision_host_class]
        commands = []
        with patch.object(infra, "host_classes", return_value=set()), infra.planning() as plan:
            infra.provision_host_class("ca_signer", lambda cmd, **kw: commands.append(cmd))
        details = " ".join(f"{kind} {detail}" for kind, detail in plan.actions) + " " + " ".join(
            " ".join(c) for c in commands)
        for expected in ("hams_relay_ca", "hams_identity_ca", "hams_capability_ca", "/opt/hams/etc/relay_ca ",
                         "/opt/hams/etc/relay_ca_public", "/opt/hams/etc/identity_ca", "/opt/hams/etc/capability_ca",
                         "hams-relay-ca.service", "hams-identity-ca.service", "hams-capability-ca.service",
                         "daemon-reload"):
            self.assertIn(expected, details)
        for unexpected in ("hams_relay_signer", "relay_ca_client", "enable", " start", "apt", "-G hams_capability_ca"):
            self.assertNotIn(unexpected, details)
        self.assertIn("usermod -a -G hams_relay_ca odoo", details)
        self.assertIn("usermod -a -G hams_identity_ca odoo", details)
        with self.assertRaises(ValueError):
            infra.provision_host_class("nonsense", lambda cmd, **kw: None)

    def test_class_gated_units_are_listed(self):
        tenant_units = {
            "hams-tenant@.service", "hams-tenant-firewall.service", "hams-tenant-backup.service",
            "hams-tenant-backup.timer", "hams-tenant-restore-test.service",
            "hams-tenant-restore-test.timer", "hams-tenant-health.service", "hams-tenant-health.timer",
            "hams-static@.service",
        }
        b2_units = {
            "hams-b2-backup.service", "hams-b2-backup.timer", "hams-b2-restore-test.service",
            "hams-b2-restore-test.timer", "hams-b2-backup-check.service", "hams-b2-backup-check.timer",
        }
        self.assertEqual(
            infra.host_class_unit_names(),
            {"hams-relay-ca.service", "hams-identity-ca.service", "hams-capability-ca.service"} | tenant_units | b2_units
        )


class B2BackupManifestTests(unittest.TestCase):
    """Tests [@ANCHOR: b2_backup:tool]: the off-site backup is for hams1 only, never a test host,
    never started by provisioning, and quiet during the nightly Odoo window."""

    def _specs(self):
        return [s for s in infra.MANIFEST["static_files"] if "b2_backup" in s["path"] or "hams-b2-" in s["path"]]

    def test_every_b2_file_unit_and_directory_is_prod_only_and_class_gated(self):
        specs = self._specs()
        self.assertEqual(len(specs), 7, [s["path"] for s in specs])
        for spec in specs:
            self.assertEqual(spec["host_class"], "b2_backup", spec["path"])
            self.assertEqual(spec["environments"], ["prod"], spec["path"])
        for path in ("/opt/hams/etc/b2_backup", "/var/lib/hams-b2-backup"):
            d = [x for x in infra.MANIFEST["directories"] if x["path"] == path][0]
            self.assertEqual((d["owner"], d["provision_mode"], d["host_class"], d["environments"]),
                             ("root:root", "700", "b2_backup", ["prod"]))
        self.assertIn("b2_backup", infra.KNOWN_HOST_CLASSES)

    def test_config_uses_the_shared_bucket_under_its_own_files_prefix(self):
        spec = [s for s in self._specs() if s["path"].endswith("b2_backup/config.json")][0]
        backend = json.loads(spec["content"].replace("{{", "{").replace("}}", "}"))["backend"]
        self.assertEqual((backend["bucket"], backend["prefix"]), ("hams-com-prod-backups", "files/"))

    def test_services_run_the_root_owned_deployed_tool(self):
        services = [s for s in self._specs() if s["path"].endswith(".service")]
        self.assertEqual(len(services), 3)
        for spec in services:
            self.assertIn("ExecStart=/usr/bin/python3 /opt/hams/src/hams_open/hams_shared/tools/b2_backup.py ",
                          spec["content"], spec["path"])
            self.assertNotIn("/opt/hams/hams_shared", spec["content"], spec["path"])

    def test_units_are_opt_in_so_provisioning_never_enables_or_starts_them(self):
        names = {os.path.basename(s["path"]) for s in self._specs() if s["path"].endswith((".service", ".timer"))}
        self.assertEqual(len(names), 6)
        self.assertTrue(names <= infra.opt_in_unit_names())
        self.assertFalse(names & infra.external_fetch_unit_names())
        self.assertFalse(infra._activation_units_to_enable(sorted(names), is_test_env=False))
        with patch.object(infra, "host_classes", return_value={"b2_backup"}):
            self.assertFalse(names & set(infra._smoketest_candidate_services()))
        with patch.object(infra, "host_classes", return_value=set()):
            self.assertFalse(names & set(infra._smoketest_candidate_services()))

    def test_backup_timer_avoids_the_nightly_odoo_window_and_the_service_is_throttled(self):
        by_name = {os.path.basename(s["path"]): s["content"] for s in self._specs()}
        timer = by_name["hams-b2-backup.timer"]
        match = re.search(r"OnCalendar=\*-\*-\* (\d\d):(\d\d):\d\d UTC", timer)
        self.assertTrue(match, timer)
        start = int(match.group(1)) * 60 + int(match.group(2))
        delay = int(re.search(r"RandomizedDelaySec=(\d+)m", timer).group(1))
        self.assertGreater(start, 20, "must start after 00:20 UTC")
        self.assertLess(start + delay, 24 * 60)
        for unit in ("hams-b2-backup.service", "hams-b2-restore-test.service"):
            text = by_name[unit]
            for line in ("Nice=19", "IOSchedulingClass=idle", "CPUQuota=100%", "ProtectSystem=strict",
                         "ConditionPathExists=/opt/hams/etc/b2_backup/b2_backup.env"):
                self.assertIn(line, text, unit)
        self.assertIn("b2_backup.py backup", by_name["hams-b2-backup.service"])
        self.assertIn("b2_backup.py restore-test", by_name["hams-b2-restore-test.service"])
        self.assertIn("b2_backup.py check", by_name["hams-b2-backup-check.service"])

    def test_shipped_config_is_valid_and_excludes_its_own_credentials(self):
        spec = [s for s in self._specs() if s["path"].endswith("b2_backup/config.json")][0]
        raw = json.loads(infra.format_env(spec["content"], {}))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w") as f:
                json.dump(raw, f)
            cfg = b2_backup.load_config(path)
        self.assertEqual(cfg.backend["endpoint"], "s3.us-east-005.backblazeb2.com")
        self.assertEqual(cfg.retention, {"daily": 14, "weekly": 8, "monthly": 12})
        etc = [p for p in cfg.paths if p["path"] == "/opt/hams/etc"][0]
        self.assertEqual(etc["exclude"], ["b2_backup/b2_backup.env"])
        # ADR 0106: the tenants live in hams_prod, so its filestore and database cover them. What is still
        # per tenant: the retired instances' archives (including tenant_ctl delete's final one) and the
        # static tree, both optional so a host without them does not fail the night.
        by_name = {p["name"]: p for p in cfg.paths}
        self.assertIn("filestore_hams_prod", by_name)
        self.assertEqual(cfg.databases, ["hams_prod"])
        self.assertEqual((by_name["tenant_archives"]["path"], by_name["tenant_archives"]["optional"]),
                         ("/opt/hams/backups/tenants", True))
        self.assertEqual((by_name["static_perens_com"]["path"], by_name["static_perens_com"]["optional"]),
                         ("/var/lib/hams-static/perens_com/static", True))
        self.assertFalse(by_name["filestore_hams_prod"]["optional"])
        self.assertEqual(cfg.env_file, "/opt/hams/etc/b2_backup/b2_backup.env")
        filestore = [p for p in cfg.paths if p["name"] == "filestore_hams_prod"][0]
        self.assertEqual(filestore["path"], "/var/lib/odoo/.local/share/Odoo/filestore/hams_prod")


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
        "dx.firehose.service": "local websocket server over the local database",
        "gdpr.csv.export.service": "local HTTP export server",
        "hamcall.idx.sync.service": "reads a licensed file already on disk; no timer",
        "hams-auth-gateway.service": "local server",
        "shack-console.service": "local static server on 127.0.0.1; serves embedded files, fetches nothing",
        "hams-pgbackrest-backup.path": "fires only on a spool file a configured backup job writes",
        "hams-pgbackrest-backup.service": "runs only for a configured backup job",
        "hams-pycache.service": "compiles local Python files",
        "hams-relay-signer.service": "local signing socket",
        "hams-subcarrier-signer.service": "local signing socket",
        "hams-static@.service": "read-only loopback file server for a tenant's static tree; no outbound network at all",
        "hams-tenant@.service": "an Odoo tenant: loopback listener, local PostgreSQL socket; fetches nothing",
        "hams-tenant-firewall.service": "loads a local nftables table",
        "hams-tenant-backup.service": "local pg_dump and tar of tenant data",
        "hams-tenant-backup.timer": "activates hams-tenant-backup.service (local)",
        "hams-tenant-health.service": "checks local units, loopback HTTP and backup age",
        "hams-tenant-health.timer": "activates hams-tenant-health.service (local)",
        "hams-tenant-restore-test.service": "restores a local backup into a scratch database",
        "hams-b2-backup.service": "uploads encrypted backups to our own B2 bucket; fetches nothing from a third party",
        "hams-b2-backup.timer": "activates hams-b2-backup.service (own bucket)",
        "hams-b2-restore-test.service": "reads back our own B2 bucket into a scratch location and database",
        "hams-b2-restore-test.timer": "activates hams-b2-restore-test.service (own bucket)",
        "hams-b2-backup-check.service": "reads two local status files",
        "hams-b2-backup-check.timer": "activates hams-b2-backup-check.service (local)",
        "hams-tenant-restore-test.timer": "activates hams-tenant-restore-test.service (local)",
        "hams.daemon.keys.service": "provisions daemon keys in the local database",
        "hams.data.relay.service": "serves map data from local Redis; no ingestion task",
        "hams.db.local.backup.service": "local pg_dump",
        "hams.db.local.backup.timer": "activates hams.db.local.backup.service (local)",
        "hams.relay.bridge.service": "local websocket bridge to the local Odoo",
        "hams.simulated.band.service": "local server; STUN only while negotiating with a peer",
        "hams.simulated.bots.service": "local band client; speech model fetched once into a cache",
        "hams.simulated.observer.service": "local band client, same as the bots",
        "pdns.sync.service": "RabbitMQ consumer writing to the local PowerDNS API",
        "hams-site-monitor.service": "probes local ports, our own site and units; posts only to the operator's own webhook or SMTP",
        "hams-site-monitor.timer": "activates hams-site-monitor.service (local)",
        "stray.odoo.shell.detector.service": "inspects local processes",
        "stray.odoo.shell.detector.timer": "activates stray.odoo.shell.detector.service (local)",
        "web.bot.auth.directory.service": "signs a file in its own directory; AF_UNIX only, no network",
        "web.bot.auth.directory.timer": "activates web.bot.auth.directory.service (local)",
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


class BootServiceUnitTests(unittest.TestCase):
    """Tests [@ANCHOR: infrastructure:boot_service_unit_names]

    Fails if a long-running production daemon would be left link-only (never started again after
    a reboot), or if a unit that needs a deliberate release step would be enabled at boot."""

    def test_the_known_production_daemons_are_enabled_at_boot(self):
        names = infra.boot_service_unit_names()
        for expected in (
            "backup.worker.service",
            "adif.ingress.service",
            "dx.firehose.service",
            "hams.simulated.band.service",
            "hams.simulated.bots.service",
            "hams.relay.bridge.service",
            "qrz.scraper.service",
        ):
            self.assertIn(expected, names)

    def test_release_time_and_custody_units_stay_link_only(self):
        names = infra.boot_service_unit_names()
        for excluded in infra.BOOT_ENABLE_EXCLUDED_SERVICES:
            self.assertNotIn(excluded, names)
        self.assertIn("hams-auth-gateway.service", infra.BOOT_ENABLE_EXCLUDED_SERVICES)
        self.assertIn("hams-relay-ca.service", infra.BOOT_ENABLE_EXCLUDED_SERVICES)

    def test_no_oneshot_template_or_opt_in_unit_is_enabled_at_boot(self):
        names = infra.boot_service_unit_names()
        by_name = {os.path.basename(spec["path"]): spec for spec in infra.MANIFEST["static_files"]}
        for name in names:
            self.assertNotIn("@", name)
            self.assertNotIn("Type=oneshot", by_name[name]["content"], name)
            self.assertIn("WantedBy=multi-user.target", by_name[name]["content"], name)
        self.assertEqual(names & infra.opt_in_unit_names(), set())

    def test_every_excluded_unit_is_a_real_manifest_unit(self):
        # A stale exclusion would silently stop excluding anything if the unit were renamed.
        by_name = {os.path.basename(spec["path"]) for spec in infra.MANIFEST["static_files"]}
        for name in infra.BOOT_ENABLE_EXCLUDED_SERVICES:
            self.assertIn(name, by_name)

    def test_provisioning_only_enables_them_outside_test_and_hold_runs(self):
        source = inspect.getsource(infra.provision_environment)
        self.assertIn(
            "boot_services = set() if (is_test_env or hold_odoo) else boot_service_unit_names()",
            source,
        )

    def test_a_production_run_enables_the_boot_services_it_links(self):
        linked = ["backup.worker.service", "odoo-fake.timer"]
        enabled = infra._activation_units_to_enable(linked, is_test_env=False)
        self.assertEqual(enabled, linked)


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

    def test_production_enables_every_linked_unit_except_opt_in_ones(self):
        # Tests [@ANCHOR: infrastructure:opt_in_unit_names]
        linked = self._linked_activation_units()
        opt_in = infra.opt_in_unit_names()
        self.assertIn("code.review.sweep.timer", opt_in)
        self.assertIn("code.review.sweep.timer", linked)
        self.assertEqual(
            infra._activation_units_to_enable(linked, is_test_env=False),
            [unit for unit in linked if unit not in opt_in],
        )

    def test_an_opt_in_unit_is_enabled_only_when_named(self):
        linked = self._linked_activation_units()
        enabled = infra._activation_units_to_enable(
            linked, is_test_env=False, opt_in_units=("code.review.sweep.timer",)
        )
        self.assertIn("code.review.sweep.timer", enabled)
        # A test environment still never enables an external-fetch unit, opted in or not.
        enabled_test = infra._activation_units_to_enable(
            linked, is_test_env=True, opt_in_units=("code.review.sweep.timer",)
        )
        self.assertNotIn("code.review.sweep.timer", enabled_test)

    def test_club_web_search_discovery_units_never_run_on_test_hosts_or_unasked(self):
        # Tests [@ANCHOR: infrastructure:external_fetch_unit_names]
        # Tests [@ANCHOR: infrastructure:opt_in_unit_names]
        # NIGHT_PLAN decision 67: a paid, third-party, search-grounded discovery pass. Both
        # the .service and the .timer must be external_fetch (never on a test host) AND opt_in
        # (never enabled in production until Bruce names it).
        units = {
            "club.web.search.discovery.service",
            "club.web.search.discovery.timer",
        }
        self.assertTrue(units <= infra.external_fetch_unit_names())
        self.assertTrue(units <= infra.opt_in_unit_names())
        linked = self._linked_activation_units()
        self.assertIn("club.web.search.discovery.timer", linked)
        for is_test_env in (True, False):
            enabled = infra._activation_units_to_enable(linked, is_test_env=is_test_env)
            self.assertFalse(units & set(enabled))
        for is_test_env in (True, False):
            started = infra._smoketest_candidate_services(True, is_test_env=is_test_env)
            self.assertNotIn("club.web.search.discovery.service", started)
        service = next(
            spec for spec in infra.MANIFEST["static_files"]
            if spec["path"].endswith("club.web.search.discovery.service")
        )
        self.assertIn(
            "ODOO_KEY_FILE=/opt/hams/etc/keys/club_search/club_web_search_discovery_service_internal.key",
            service["content"],
        )
        self.assertIn("--max-requests-per-day=", service["content"])

    def test_smoketest_never_starts_an_opt_in_service_unless_named(self):
        self.assertNotIn(
            "code.review.sweep.service", infra._smoketest_candidate_services(True, is_test_env=False)
        )
        self.assertIn(
            "code.review.sweep.service",
            infra._smoketest_candidate_services(
                True, is_test_env=False, opt_in_units=("code.review.sweep.service",)
            ),
        )

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
            "ncvec.sync.service",
            infra.systemd_units_running_as(infra.MANIFEST, "hamsd_ncvec_sync"),
        )


class FamilyAccountUnitTests(_SafePatchTestCase):
    """Tests [@ANCHOR: infrastructure:family_account_units]: a unit that has left the shared `odoo`
    account is held to the account, directory and environment rules of the isolation plan."""

    SECRET_NAME = re.compile(r"(PASSWORD|PASS|TOKEN|SECRET|KEY)$")

    def _directories_owned_by(self, user):
        return {
            d["path"]
            for d in infra.MANIFEST["directories"]
            if d.get("owner", "").split(":")[0] == user
        }

    def test_each_migrated_unit_runs_as_and_in_the_group_of_its_account(self):
        for unit, rules in infra.FAMILY_ACCOUNT_UNITS.items():
            with self.subTest(unit=unit):
                user = rules["user"]
                self.assertEqual(infra.systemd_unit_directive_values(infra.MANIFEST, unit, "User"), [user])
                self.assertEqual(infra.systemd_unit_directive_values(infra.MANIFEST, unit, "Group"), [user])
                self.assertIn(unit, infra.systemd_units_running_as(infra.MANIFEST, user))
                self.assertNotIn(unit, infra.systemd_units_running_as(infra.MANIFEST, "odoo"))
                self.assertNotIn(unit, infra.SHARED_ODOO_ACCOUNT_UNITS)

    def test_every_unit_running_as_a_daemon_family_account_is_listed_with_its_rules(self):
        accounts = {a["user"] for a in infra.MANIFEST["system_accounts"] if a["user"].startswith("hamsd_")}
        running = set()
        for account in accounts:
            running |= infra.systemd_units_running_as(infra.MANIFEST, account)
        self.assertEqual(
            sorted(running), sorted(infra.FAMILY_ACCOUNT_UNITS),
            "A unit under a hamsd_ account must be in FAMILY_ACCOUNT_UNITS so the narrow-write and "
            "environment rules apply to it.",
        )

    def test_a_daemon_family_account_is_provisioned_the_way_the_key_flow_needs(self):
        for unit, rules in infra.FAMILY_ACCOUNT_UNITS.items():
            with self.subTest(unit=unit):
                matches = [a for a in infra.MANIFEST["system_accounts"] if a["user"] == rules["user"]]
                self.assertEqual(len(matches), 1)
                account = matches[0]
                self.assertEqual(account["group"], rules["user"])
                self.assertEqual(account["shell"], "/usr/sbin/nologin")
                # odoo must be in the group to hand it a key file (chgrp, never chown); a unit with no key
                # file has nothing to hand over and odoo is not in its group.
                if rules.get("no_key") and not rules.get("odoo_reads"):
                    self.assertNotIn("odoo", account.get("add_to_users", []))
                else:
                    # `odoo_reads`: no key to hand over, but odoo reads what the unit writes (group read, never write).
                    self.assertIn("odoo", account["add_to_users"])
                # The traversal group (not hams_com) is what lets the account reach /opt/hams, and the
                # account is taken out of hams_com so it cannot read the ADIF queue or other hams_com data.
                self.assertEqual(account["member_of"], ["hams_traverse"])
                self.assertEqual(account["not_member_of"], ["hams_com"])
                self.assertEqual(account["environments"], ["prod", "test"])

    def test_the_writable_paths_are_only_directories_the_account_owns(self):
        for unit, rules in infra.FAMILY_ACCOUNT_UNITS.items():
            with self.subTest(unit=unit):
                writable = infra.systemd_unit_directive_values(infra.MANIFEST, unit, "ReadWritePaths")
                if rules.get("no_state"):
                    # A daemon that fetches and pushes to Odoo and writes no file has no writable path at all.
                    self.assertEqual(writable, [])
                    continue
                self.assertTrue(writable, "a oneshot daemon with no writable path cannot spool anything")
                if rules.get("agent_sudo"):
                    # The documented exception: the agent's own home and nothing else (the CLI, running
                    # as the agent, writes its session state there; the daemon's account never does).
                    agent = rules["agent_sudo"]
                    self.assertEqual(writable, [f"/home/{agent}"])
                    self.assertIn(f"/home/{agent}", self._directories_owned_by(agent))
                    continue
                owned = self._directories_owned_by(rules["user"])
                self.assertEqual(sorted(set(writable) - owned), [])
                for shared in ("/opt/hams/spool", "/opt/hams/downloads", "/opt/hams/cache", "/opt/hams"):
                    self.assertNotIn(shared, writable)

    def test_the_state_directories_are_private_to_the_account_and_readable_by_odoo(self):
        for unit, rules in infra.FAMILY_ACCOUNT_UNITS.items():
            if rules.get("agent_sudo"):
                continue  # its only writable path is the agent's home, checked above
            for path in infra.systemd_unit_directive_values(infra.MANIFEST, unit, "ReadWritePaths"):
                with self.subTest(unit=unit, path=path):
                    entry = [d for d in infra.MANIFEST["directories"] if d["path"] == path][0]
                    self.assertEqual(entry["owner"], f"{rules['user']}:{rules['user']}")
                    self.assertEqual(entry["provision_mode"], "750")
                    self.assertTrue(entry["recursive_owner"])
                    self.assertEqual(entry["environments"], ["prod", "test"])

    def test_a_migrated_unit_loads_only_the_environment_files_it_uses(self):
        for unit, rules in infra.FAMILY_ACCOUNT_UNITS.items():
            with self.subTest(unit=unit):
                loaded = {
                    os.path.basename(value.lstrip("-"))
                    for value in infra.systemd_unit_directive_values(infra.MANIFEST, unit, "EnvironmentFile")
                }
                self.assertEqual(loaded, set(rules["environment_files"]))
                self.assertLessEqual(loaded - set(rules.get("operator_env_files", ())), set(infra.MANIFEST["env_groups"]))

    def test_common_env_carries_no_secret(self):
        keys = infra.MANIFEST["env_groups"]["common.env"]
        self.assertEqual(sorted(k for k in keys if self.SECRET_NAME.search(k)), [])
        # A value duplicated here must stay in its own file too: no existing unit changes behavior.
        for key in keys:
            self.assertTrue(
                any(key in values for name, values in infra.MANIFEST["env_groups"].items() if name != "common.env"),
                f"{key} is only in common.env: remove the original deliberately, in its own change",
            )

    def test_the_key_directory_is_traversable_by_hams_com_and_not_listable(self):
        entry = [d for d in infra.MANIFEST["directories"] if d["path"] == "/opt/hams/etc/keys"][0]
        self.assertEqual((entry["owner"], entry["provision_mode"]), ("odoo:hams_com", "710"))

    def test_a_migrated_unit_keeps_the_sandbox_and_adds_the_isolation_directives(self):
        required = {
            "ProtectSystem": ["strict"],
            "PrivateTmp": ["true"],
            "NoNewPrivileges": ["true"],
            "ProtectProc": ["invisible"],
            "ProcSubset": ["pid"],
            "ProtectKernelTunables": ["true"],
            "RestrictSUIDSGID": ["true"],
            "LockPersonality": ["true"],
        }
        for unit, rules in infra.FAMILY_ACCOUNT_UNITS.items():
            for directive, expected in required.items():
                if rules.get("agent_sudo") and directive == "NoNewPrivileges":
                    # sudo's uid switch cannot work under NoNewPrivileges=true: the unit omits it and
                    # keeps only the two capabilities the switch needs.
                    with self.subTest(unit=unit, directive=directive):
                        self.assertEqual(infra.systemd_unit_directive_values(infra.MANIFEST, unit, directive), [])
                        self.assertEqual(
                            infra.systemd_unit_directive_values(infra.MANIFEST, unit, "CapabilityBoundingSet"),
                            ["CAP_SETUID", "CAP_SETGID"],
                        )
                    continue
                with self.subTest(unit=unit, directive=directive):
                    self.assertEqual(
                        infra.systemd_unit_directive_values(infra.MANIFEST, unit, directive), expected
                    )

    def test_an_agent_sudo_unit_reaches_the_agent_through_one_sudoers_line_and_nothing_wider(self):
        for unit, rules in infra.FAMILY_ACCOUNT_UNITS.items():
            agent = rules.get("agent_sudo")
            if not agent:
                continue
            with self.subTest(unit=unit):
                lines = [
                    entry for entry in infra.MANIFEST["static_files"]
                    if entry["path"].startswith("/etc/sudoers.d/")
                    and any(l.startswith(rules["user"] + " ") for l in entry["content"].splitlines())
                ]
                self.assertEqual(len(lines), 1)
                grant = lines[0]
                self.assertEqual(
                    grant["content"],
                    f"{rules['user']} ALL=({agent}) NOPASSWD: /usr/local/sbin/run-hams-ai-agent-claude.sh\n",
                )
                self.assertEqual((grant["owner"], grant["mode"], grant["environments"]), ("root:root", "440", ["prod"]))
                self.assertIn("run-hams-ai-agent-claude.sh", [
                    e["path"].rsplit("/", 1)[-1] for e in infra.MANIFEST["static_files"]
                ])
                # The account is not in the sudo group and gets no blanket grant anywhere.
                for entry in infra.MANIFEST["static_files"]:
                    if entry["path"].startswith("/etc/sudoers.d/"):
                        for line in entry["content"].splitlines():
                            if line.startswith(rules["user"] + " "):
                                self.assertNotIn("ALL=(ALL)", line)
                account = [a for a in infra.MANIFEST["system_accounts"] if a["user"] == rules["user"]][0]
                self.assertNotIn("sudo", account.get("member_of", []))
                # The unit is prod-only and opt-in: the agent account and wrapper exist only on hams1.
                entry = [e for e in infra.MANIFEST["static_files"] if e["path"].endswith("/" + unit)][0]
                self.assertEqual(entry["environments"], ["prod"])
                self.assertTrue(entry.get("opt_in"))
                self.assertTrue(entry.get("external_fetch"))

    def test_the_key_file_a_migrated_unit_reads_is_under_the_key_directory(self):
        for unit, rules in infra.FAMILY_ACCOUNT_UNITS.items():
            with self.subTest(unit=unit):
                if rules.get("no_key"):
                    # A unit that holds no Odoo key (dx.firehose connects to the database itself): none is named.
                    values = infra.systemd_unit_directive_values(infra.MANIFEST, unit, "Environment")
                    self.assertEqual([v for v in values if "ODOO_KEY_FILE" in v], [])
                    continue
                values = infra.systemd_unit_directive_values(infra.MANIFEST, unit, "Environment")
                key_files = [v.strip('"').split("=", 1)[1] for v in values if v.strip('"').startswith("ODOO_KEY_FILE=")]
                self.assertEqual(len(key_files), 1)
                self.assertTrue(key_files[0].startswith("/opt/hams/etc/keys/"))

    def test_each_family_has_its_own_key_directory_owned_by_odoo_with_the_family_group(self):
        """Bruce, NIGHT_PLAN 226: key directories are owned by odoo with group = the consuming daemon
        account, 0750, and the key file the unit reads is in its family's directory."""
        for unit, rules in infra.FAMILY_ACCOUNT_UNITS.items():
            with self.subTest(unit=unit):
                user = rules["user"]
                family = user[len("hamsd_"):]
                directory = f"/opt/hams/etc/keys/{family}"
                if rules.get("no_key"):
                    self.assertEqual([d for d in infra.MANIFEST["directories"] if d["path"] == directory], [])
                    continue
                entries = [d for d in infra.MANIFEST["directories"] if d["path"] == directory]
                self.assertEqual(len(entries), 1)
                self.assertEqual((entries[0]["owner"], entries[0]["provision_mode"]), (f"odoo:{user}", "750"))
                self.assertEqual(entries[0].get("environments"), ["prod", "test"])
                # odoo must be a member of the group, or it cannot chgrp the directory or the key files.
                account = [a for a in infra.MANIFEST["system_accounts"] if a["user"] == user][0]
                self.assertIn("odoo", account.get("add_to_users", []))
                values = infra.systemd_unit_directive_values(infra.MANIFEST, unit, "Environment")
                key_file = [v.strip('"').split("=", 1)[1] for v in values if v.strip('"').startswith("ODOO_KEY_FILE=")][0]
                self.assertEqual(os.path.dirname(key_file), directory)

    def test_no_key_file_is_read_by_the_units_of_two_different_accounts(self):
        """The reason for one account per key: a key file readable by two accounts isolates nothing."""
        readers = {}
        for unit, rules in infra.FAMILY_ACCOUNT_UNITS.items():
            values = infra.systemd_unit_directive_values(infra.MANIFEST, unit, "Environment")
            for v in values:
                v = v.strip('"')
                if v.startswith("ODOO_KEY_FILE="):
                    readers.setdefault(v.split("=", 1)[1], set()).add(rules["user"])
        for key_file, users in readers.items():
            with self.subTest(key_file=key_file):
                self.assertEqual(len(users), 1, f"{key_file} is read by {sorted(users)}")

    def test_the_hamcall_directory_belongs_to_the_one_daemon_that_reads_it(self):
        entry = [d for d in infra.MANIFEST["directories"] if d["path"] == "/opt/hams/hamcall"][0]
        self.assertEqual((entry["owner"], entry["provision_mode"]), ("hamsd_hamcall_sync:hamsd_hamcall_sync", "750"))
        self.assertTrue(entry["recursive_owner"])
        values = infra.systemd_unit_directive_values(infra.MANIFEST, "hamcall.idx.sync.service", "Environment")
        self.assertIn('"HAMCALL_IDX_PATH=/opt/hams/hamcall/hamcall.idx"', values)

    def test_au_pii_has_its_own_key_not_the_country_syncs(self):
        pii = infra.systemd_unit_directive_values(infra.MANIFEST, "au.pii.sync.service", "Environment")
        country = infra.systemd_unit_directive_values(infra.MANIFEST, "au.acma.sync.service", "Environment")
        self.assertIn('"ODOO_KEY_FILE=/opt/hams/etc/keys/au_pii/au_pii_sync_service_internal.key"', pii)
        self.assertIn('"ODOO_KEY_FILE=/opt/hams/etc/keys/country_sync/callbook_sync_service_internal.key"', country)

    def test_the_key_root_stays_traversable_only_and_no_family_directory_is_group_writable(self):
        paths = {d["path"]: d for d in infra.MANIFEST["directories"]}
        self.assertEqual(paths["/opt/hams/etc/keys"]["provision_mode"], "710")
        for path, entry in paths.items():
            if path.startswith("/opt/hams/etc/keys/"):
                with self.subTest(path=path):
                    self.assertTrue(entry["owner"].startswith("odoo:"))
                    self.assertEqual(int(entry["provision_mode"], 8) & 0o027, 0)


class ProvisionDaemonFamiliesTests(_SafePatchTestCase):
    """Tests [@ANCHOR: infrastructure:daemon_family_selection] and
    [@ANCHOR: infrastructure:provision_daemon_families]: `provision.py --daemon-family` touches one
    family's entries and nothing else."""

    def _plan(self, accounts, env_exists=True):
        commands = []
        real_exists = os.path.exists
        self.safe_patch_object(
            infra.os.path, "exists",
            side_effect=lambda p: True if p.endswith("/common.env") and env_exists else real_exists(p),
        )
        # Nothing exists on this machine for the planner to find, so every entry is "new".
        with infra.planning() as plan:
            infra.provision_daemon_families(
                accounts, lambda cmd, **kw: commands.append(list(cmd)), {"DOMAIN": "example.test"}
            )
        return plan.actions, commands

    def test_only_the_named_familys_entries_are_selected(self):
        actions, commands = self._plan(["hamsd_ncvec_sync"])
        details = " ".join(detail for _kind, detail in actions) + " " + " ".join(" ".join(c) for c in commands)
        self.assertIn("hamsd_ncvec_sync", details)
        self.assertIn("/opt/hams/systemd/ncvec.sync.service", details)
        self.assertIn("/opt/hams/systemd/ncvec.sync.timer", details)
        self.assertIn("/opt/hams/etc/keys/ncvec_sync", details)
        for other in ("club_crawl", "club-crawl", "pota.sync", "hams_subcarrier_signer", "pdns", "odoo.service"):
            self.assertNotIn(other, details, f"{other} is not part of the ncvec_sync family")

    def test_a_sudoers_grant_and_the_shared_key_root_come_with_the_family_that_needs_them(self):
        actions, commands = self._plan(["hamsd_club_crawl"])
        details = " ".join(detail for _kind, detail in actions)
        self.assertIn("/etc/sudoers.d/club-crawl-claude-sandbox", details)
        self.assertIn("/opt/hams/etc/keys (odoo:hams_com, 710)", details)

    def test_nothing_is_enabled_started_or_restarted(self):
        actions, commands = self._plan(["hamsd_ncvec_sync"])
        for command in commands:
            self.assertNotIn(command[:2], (["systemctl", "enable"], ["systemctl", "start"], ["systemctl", "restart"]))
        self.assertEqual([c for c in commands if c[0] == "systemctl"], [])
        self.assertEqual([d for kind, d in actions if kind == "run"], ["systemctl daemon-reload"])

    def test_a_missing_required_environment_file_is_refused_before_anything_is_written(self):
        written = []
        real_exists = os.path.exists
        self.safe_patch_object(
            infra.os.path, "exists",
            side_effect=lambda p: False if p.endswith("/common.env") else real_exists(p),
        )
        self.safe_patch_object(infra, "provision_system_accounts", side_effect=lambda *a, **k: written.append("accounts"))
        self.safe_patch_object(infra, "_derived_env_values", return_value=None)
        with self.assertRaisesRegex(RuntimeError, "common.env"):
            infra.provision_daemon_families(["hamsd_ncvec_sync"], lambda cmd, **kw: written.append(cmd), {})
        self.assertEqual(written, [])

    def test_an_account_that_is_not_a_daemon_family_is_refused(self):
        for bad in ("odoo", "hams_com", "hamsd_no_such_family"):
            with self.subTest(account=bad):
                with self.assertRaises(ValueError):
                    infra.provision_daemon_families([bad], lambda cmd, **kw: None, {})

    def test_the_selection_is_off_again_after_the_block(self):
        self._plan(["hamsd_ncvec_sync"])
        self.assertIsNone(infra._DAEMON_FAMILY_ACCOUNTS)
        self.assertTrue(infra._spec_selected({"path": "/x", "environments": ["prod"]}))

    def test_every_unit_of_every_family_account_is_found_by_the_selection(self):
        accounts = [a["user"] for a in infra.MANIFEST["system_accounts"] if a["user"].startswith("hamsd_")]
        found = infra.daemon_family_unit_paths(accounts)
        for unit in infra.FAMILY_ACCOUNT_UNITS:
            self.assertIn(f"/opt/hams/systemd/{unit}", found)


class DerivedEnvFileTests(_TmpDirTestCase):
    """Tests [@ANCHOR: infrastructure:derived_env_files]: an env group that is a split of others is cut from them."""

    def _etc(self, files):
        for name, text in files.items():
            with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
                f.write(text)
        return os.path.join(self.tmp, "common.env")

    def test_common_env_is_cut_from_core_odoo_and_db_env(self):
        target = self._etc({
            "core.env": "DOMAIN=example.test\nSYSTEM_USER_AGENT=Agent/1 (+x; y)\nHAMS_CRYPTO_KEY=secretsecret\n",
            "odoo.env": "ODOO_ADMIN_PASSWORD=adminsecret\nODOO_URL=http://odoo:8069\n",
            "db.env": "DB_NAME=hams_prod\nPOSTGRES_PASSWORD=pgsecret\n",
        })
        values = infra._derived_env_values(target)
        self.assertEqual(values, {
            "DOMAIN": "example.test", "SYSTEM_USER_AGENT": "Agent/1 (+x; y)",
            "ODOO_URL": "http://odoo:8069", "DB_NAME": "hams_prod",
        })

    def test_a_key_with_no_source_means_the_file_cannot_be_derived(self):
        target = self._etc({"core.env": "DOMAIN=example.test\n"})
        self.assertIsNone(infra._derived_env_values(target))

    def test_a_file_that_is_not_an_env_group_is_never_derived(self):
        self._etc({"core.env": "DOMAIN=example.test\n"})
        self.assertIsNone(infra._derived_env_values(os.path.join(self.tmp, "aws.env")))

    def test_the_derived_file_is_private_and_holds_exactly_the_group_keys(self):
        target = self._etc({"db.env": "DB_NAME=n\nPOSTGRES_PASSWORD=super\nDB_PASS=app\nDB_HOST=h\nDB_PORT=5\nDB_USER=u\n"})
        target = os.path.join(self.tmp, "db_app.env")
        values = infra._derived_env_values(target)
        self.safe_patch_object(infra, "apply_permissions")
        infra._write_derived_env_file(target, values)
        with open(target, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(stat_mode(target), 0o400)
        self.assertNotIn("POSTGRES_PASSWORD", text)
        self.assertNotIn("super", text)
        self.assertEqual(sorted(line.split("=")[0] for line in text.splitlines()), sorted(infra.MANIFEST["env_groups"]["db_app.env"]))

    def test_the_application_role_file_never_carries_the_superuser_password(self):
        groups = infra.MANIFEST["env_groups"]
        self.assertNotIn("POSTGRES_PASSWORD", groups["db_app.env"])
        for key in groups["db_app.env"]:
            self.assertIn(key, groups["db.env"], f"{key} must also stay in db.env: no existing unit changes")

    def test_provisioning_plans_the_derived_file_instead_of_refusing(self):
        real_exists = os.path.exists
        self.safe_patch_object(infra.os.path, "exists", side_effect=lambda p: False if p.endswith("/common.env") else real_exists(p))
        self.safe_patch_object(infra, "_derived_env_values", return_value={"DOMAIN": "x", "ODOO_URL": "y", "DB_NAME": "z", "SYSTEM_USER_AGENT": "a"})
        with infra.planning() as plan:
            infra.provision_daemon_families(["hamsd_ncvec_sync"], lambda cmd, **kw: None, {})
        writes = [d for kind, d in plan.actions if kind == "write" and "derived" in d]
        self.assertEqual(len(writes), 1)
        self.assertIn("/opt/hams/etc/common.env", writes[0])
        self.assertNotIn("example", writes[0])
        self.assertEqual([d for kind, d in plan.actions if kind == "missing"], [])


class BrowserFetchingFamilyUnitTests(_SafePatchTestCase):
    """uk.ofcom.sync fetches the Ofcom file through Chromium (daemons/uk_ofcom_sync fetch_with_browser):
    it must find the shared Playwright install, reach it as a family account, and still write nothing
    outside its own directories."""

    UNIT = "uk.ofcom.sync.service"
    BROWSERS = "/opt/hams/cache/ms-playwright"

    def _unit_text(self):
        for entry in infra.MANIFEST["static_files"]:
            if entry["path"].endswith("/" + self.UNIT):
                return entry["content"]
        self.fail("unit not found")

    def test_the_unit_points_playwright_at_the_shared_install(self):
        self.assertIn(f'Environment="PLAYWRIGHT_BROWSERS_PATH={self.BROWSERS}"', self._unit_text())

    def test_the_install_is_reachable_by_the_family_account_and_never_writable_by_it(self):
        dirs = {d["path"]: d for d in infra.MANIFEST["directories"]}
        for path in ("/opt/hams/cache", self.BROWSERS):
            self.assertEqual(dirs[path]["acl"], ["g:hams_traverse:--x"])
            self.assertEqual(dirs[path]["owner"], "hams_com:hams_com")
        writable = infra.systemd_unit_directive_values(infra.MANIFEST, self.UNIT, "ReadWritePaths")
        self.assertNotIn(self.BROWSERS, writable)
        self.assertNotIn("/opt/hams/cache", writable)

    def test_the_other_cache_directories_are_not_opened_up(self):
        for d in infra.MANIFEST["directories"]:
            if d["path"].startswith("/opt/hams/cache/") and d["path"] != self.BROWSERS:
                self.assertNotIn("acl", d, d["path"])


class TraversalGrantTests(_SafePatchTestCase):
    """Tests [@ANCHOR: infrastructure:directory_acl]: a daemon family account reaches its own paths
    through an execute-only ACL for hams_traverse, not through hams_com."""

    GRANTED = [
        "/opt/hams", "/opt/hams/etc", "/opt/hams/etc/keys", "/opt/hams/spool", "/opt/hams/downloads",
        "/opt/hams/cache", "/opt/hams/cache/ms-playwright",
    ]

    def _dirs(self):
        return {d["path"]: d for d in infra.MANIFEST["directories"]}

    def test_the_directories_a_family_passes_through_grant_execute_only_to_the_traversal_group(self):
        dirs = self._dirs()
        for path in self.GRANTED:
            with self.subTest(path=path):
                self.assertEqual(dirs[path]["acl"], ["g:hams_traverse:--x"])

    def test_nothing_else_carries_an_acl_and_no_grant_is_more_than_execute(self):
        for d in infra.MANIFEST["directories"]:
            for entry in d.get("acl", []):
                with self.subTest(path=d["path"], entry=entry):
                    self.assertIn(d["path"], self.GRANTED)
                    self.assertTrue(entry.endswith(":--x"), "a traversal grant must never read or write")

    def test_the_traversal_group_has_no_member_but_the_family_accounts_and_is_created_first(self):
        accounts = infra.MANIFEST["system_accounts"]
        names = [a["user"] for a in accounts]
        traverse = accounts[names.index("hams_traverse")]
        self.assertEqual((traverse["group"], traverse["shell"]), ("hams_traverse", "/usr/sbin/nologin"))
        self.assertNotIn("add_to_users", traverse)
        for acc in accounts:
            if acc["user"].startswith("hamsd_"):
                self.assertLess(names.index("hams_traverse"), names.index(acc["user"]))

    def test_no_family_account_is_in_hams_com(self):
        for acc in infra.MANIFEST["system_accounts"]:
            if acc["user"].startswith("hamsd_"):
                with self.subTest(account=acc["user"]):
                    self.assertNotIn("hams_com", acc.get("member_of", []))
                    self.assertIn("hams_com", acc["not_member_of"])

    def test_a_directory_acl_runs_setfacl_in_production_and_is_planned_without_running(self):
        calls = []
        spec = {"path": "/opt/hams/x", "owner": None, "provision_mode": "750", "environments": ["prod"],
                "acl": ["g:hams_traverse:--x"]}
        self.safe_patch_object(infra.shutil, "which", return_value="/usr/bin/setfacl")
        infra._apply_directory_acl(spec, "/opt/hams/x", "prod", calls.append)
        self.assertEqual(calls, [["setfacl", "-m", "g:hams_traverse:--x", "/opt/hams/x"]])
        calls.clear()
        infra._apply_directory_acl(spec, "/opt/hams/x", "test", calls.append)
        self.assertEqual(calls, [], "a test host does not need the grant and may not have setfacl")
        with infra.planning() as plan:
            infra._apply_directory_acl(spec, "/opt/hams/x", "prod", calls.append)
        self.assertEqual(calls, [])
        self.assertEqual(plan.actions, [("setfacl", "/opt/hams/x: g:hams_traverse:--x")])

    def test_a_missing_setfacl_is_refused_with_the_package_name(self):
        spec = {"path": "/opt/hams/x", "acl": ["g:hams_traverse:--x"]}
        self.safe_patch_object(infra.shutil, "which", return_value=None)
        with self.assertRaisesRegex(RuntimeError, "apt-get install acl"):
            infra._apply_directory_acl(spec, "/opt/hams/x", "prod", lambda c: None)

    def test_the_acl_package_is_installed_by_provisioning(self):
        self.assertIn("acl", [p["debian_name"] for p in infra.MANIFEST["apt_packages"]])

    def test_not_member_of_removes_only_an_existing_membership(self):
        account = [{"user": "hamsd_x", "group": "hamsd_x", "not_member_of": ["hams_com"], "environments": ["prod"]}]
        for members, expected in ((["odoo", "hamsd_x"], [["gpasswd", "-d", "hamsd_x", "hams_com"]]), (["odoo"], [])):
            with self.subTest(members=members):
                calls = []
                patcher = patch.dict(infra.MANIFEST, {"system_accounts": account})
                patcher.start()
                self.addCleanup(patcher.stop)
                self.safe_patch_object(infra.pwd, "getpwnam", return_value=MagicMock())
                self.safe_patch_object(infra.grp, "getgrnam", return_value=MagicMock(gr_mem=members))
                infra.provision_system_accounts(calls.append, environment="prod")
                self.assertEqual([c for c in calls if c[0] == "gpasswd"], expected)


class SystemAccountMemberOfTests(_SafePatchTestCase):
    def _provision(self, accounts):
        calls = []
        self.safe_patch_dict(infra.MANIFEST, {"system_accounts": accounts})
        self.safe_patch_object(infra.pwd, "getpwnam", side_effect=KeyError("no such user"))
        self.safe_patch_object(infra.grp, "getgrnam", side_effect=KeyError("no such group"))
        infra.provision_system_accounts(calls.append, environment="prod")
        return calls

    def safe_patch_dict(self, target, values):
        patcher = patch.dict(target, values)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_member_of_adds_the_account_to_each_existing_group_after_creating_it(self):
        calls = self._provision([{
            "user": "hamsd_example", "group": "hamsd_example", "shell": "/usr/sbin/nologin",
            "member_of": ["hams_com", "other_group"], "environments": ["prod"],
        }])
        self.assertEqual(calls[0], ["groupadd", "--system", "hamsd_example"])
        self.assertEqual(calls[1][0], "useradd")
        self.assertEqual(calls[2:], [
            ["usermod", "-a", "-G", "hams_com", "hamsd_example"],
            ["usermod", "-a", "-G", "other_group", "hamsd_example"],
        ])

    def test_an_account_without_member_of_gets_no_extra_usermod(self):
        calls = self._provision([{
            "user": "plain", "group": "plain", "environments": ["prod"],
        }])
        self.assertEqual([c[0] for c in calls], ["groupadd", "useradd"])


class RecursiveOwnerTests(_TmpDirTestCase):
    """apply_permissions(recursive=True) re-owns what an earlier run left below a state directory."""

    def _fake_account(self):
        self.safe_patch_object(infra.pwd, "getpwnam", return_value=MagicMock(pw_uid=4242))
        self.safe_patch_object(infra.grp, "getgrnam", return_value=MagicMock(gr_gid=4343))

    def test_every_entry_below_is_re_owned_and_symlinks_are_not_followed(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        with open(os.path.join(outside, "victim"), "w", encoding="utf-8") as victim:
            victim.write("x")
        top = os.path.join(self.tmp, "state")
        os.makedirs(os.path.join(top, "images"))
        with open(os.path.join(top, "images", "a.png"), "w", encoding="utf-8") as png:
            png.write("x")
        os.symlink(outside, os.path.join(top, "escape"))
        self._fake_account()
        chowned = []
        self.safe_patch_object(infra.os, "chown", side_effect=lambda p, u, g: chowned.append(("chown", p, u, g)))
        self.safe_patch_object(infra.os, "lchown", side_effect=lambda p, u, g: chowned.append(("lchown", p, u, g)))
        infra.apply_permissions(top, "acct:acct", 0o750, recursive=True)
        self.assertIn(("chown", top, 4242, 4343), chowned)
        lchowned = {path for kind, path, _u, _g in chowned if kind == "lchown"}
        self.assertEqual(
            lchowned,
            {
                os.path.join(top, "images"),
                os.path.join(top, "images", "a.png"),
                os.path.join(top, "escape"),
            },
        )
        self.assertFalse(any(p.startswith(outside) for _k, p, _u, _g in chowned), "a symlink target was followed")
        self.assertEqual(stat_mode(top), 0o750)
        self.assertNotEqual(stat_mode(os.path.join(top, "images")), 0o750, "children keep their own mode")

    def test_without_recursive_only_the_directory_itself_is_touched(self):
        top = os.path.join(self.tmp, "state")
        os.makedirs(os.path.join(top, "images"))
        self._fake_account()
        chowned = []
        self.safe_patch_object(infra.os, "chown", side_effect=lambda p, u, g: chowned.append(p))
        self.safe_patch_object(infra.os, "lchown", side_effect=lambda p, u, g: chowned.append(p))
        infra.apply_permissions(top, "acct:acct", 0o750)
        self.assertEqual(chowned, [top])

    def test_apply_production_directories_passes_recursive_owner_through(self):
        directories = [
            {"path": "/opt/hams/x_state", "owner": "acct:acct", "provision_mode": "750",
             "recursive_owner": True, "environments": ["prod"]},
            {"path": "/opt/hams/y_plain", "owner": "acct:acct", "provision_mode": "750",
             "environments": ["prod"]},
        ]
        self.safe_patch_object(infra, "apply_permissions")
        with patch.dict(infra.MANIFEST, {"directories": directories}):
            infra.apply_production_directories(environment="prod", dest_dir=self.tmp)
        flags = {call.args[0].rsplit("/", 1)[1]: call.kwargs["recursive"] for call in infra.apply_permissions.call_args_list}
        self.assertEqual(flags, {"x_state": True, "y_plain": False})


def stat_mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class RedactCommandTests(unittest.TestCase):
    def test_masks_psql_password_variables_and_rabbitmq_passwords(self):
        # Tests [@ANCHOR: infrastructure:redact_command]
        self.assertEqual(
            infra.redact_command(["sudo", "-u", "postgres", "psql", "-v", "role_name=x", "-v", "db_pass=s3cret"]),
            ["sudo", "-u", "postgres", "psql", "-v", "role_name=x", "-v", "db_pass=<redacted>"],
        )
        self.assertEqual(
            infra.redact_command(["rabbitmqctl", "change_password", "hams", "s3cret"]),
            ["rabbitmqctl", "change_password", "hams", "<redacted>"],
        )
        self.assertEqual(infra.redact_command(["systemctl", "restart", "x"]), ["systemctl", "restart", "x"])


class AptInstallSafetyTests(_TmpDirTestCase):
    """Round-2 production runbook, 2026-10-03: a re-run's `apt-get install -y <MANIFEST list>` aborted
    on hams1's `apt-mark hold odoo`, and without the hold would have upgraded odoo, pgbackrest and
    pgvector and replaced the installed Rust toolchain."""

    def test_filter_skips_installed_held_and_toolchain_packages(self):
        # Tests [@ANCHOR: infrastructure:_filter_apt_install]
        to_install, skipped = infra._filter_apt_install(
            ["odoo", "pgbackrest", "cargo-web", "redis-server", "python3-new", "python3-new"],
            installed={"pgbackrest", "redis-server"},
            held={"odoo"},
            have_cargo=True,
        )
        self.assertEqual(to_install, ["python3-new"])
        self.assertEqual(skipped["odoo"], "held (apt-mark hold)")
        self.assertEqual(skipped["pgbackrest"], "already installed")
        self.assertIn("cargo", skipped["cargo-web"])
        to_install, _ = infra._filter_apt_install(["cargo-web"], set(), set(), have_cargo=False)
        self.assertEqual(to_install, ["cargo-web"])

    def test_parse_apt_simulation(self):
        # Tests [@ANCHOR: infrastructure:_parse_apt_simulation]
        out = (
            "Inst libfoo1 (1.2 Debian:13 [amd64])\n"
            "Inst odoo [19.0.20260923] (19.0.20261002 Odoo [all])\n"
            "Remv rustc-web [1.96]\n"
            "Conf libfoo1 (1.2 Debian:13 [amd64])\n"
        )
        self.assertEqual(infra._parse_apt_simulation(out), (["libfoo1"], ["odoo"], ["rustc-web"]))

    def _fake_probes(self, installed="", held="", simulation="", sim_rc=0):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "dpkg-query":
                return subprocess.CompletedProcess(cmd, 0, stdout=installed, stderr="")
            if cmd[:2] == ["apt-mark", "showhold"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=held, stderr="")
            if cmd[:2] == ["apt-get", "-s"]:
                return subprocess.CompletedProcess(cmd, sim_rc, stdout=simulation, stderr="E: held")
            raise AssertionError(f"unexpected command {cmd}")
        self.safe_patch_object(infra.subprocess, "run", side_effect=fake_run)
        self.safe_patch_object(infra.shutil, "which", return_value="/usr/bin/cargo")

    def test_installs_only_missing_packages_with_no_upgrade(self):
        # Tests [@ANCHOR: infrastructure:_apt_install_missing]
        # Tests [@ANCHOR: infrastructure:_installed_apt_packages]
        # Tests [@ANCHOR: infrastructure:_held_apt_packages]
        self._fake_probes(installed="ii  odoo\nii  pgbackrest\nrc  oldpkg\n", held="odoo\n",
                          simulation="Inst python3-new (1 Debian [all])\n")
        run_cmd = MagicMock()
        installed = infra._apt_install_missing(
            run_cmd, ["odoo", "pgbackrest", "python3-new", "cargo-web"], ["-o", "x"], "t"
        )
        self.assertEqual(installed, ["python3-new"])
        run_cmd.assert_called_once_with(["apt-get", "install", "-y", "--no-upgrade", "-o", "x", "python3-new"])

    def test_refuses_an_install_that_would_remove_or_upgrade_a_protected_package(self):
        for simulation in ("Remv rustc-web [1.96]\nInst rustc (1.85 Debian [amd64])\n",
                           "Inst postgresql-18-pgvector [0.8.0] (0.8.1 PGDG [amd64])\nInst python3-new (1)\n"):
            with self.subTest(simulation=simulation):
                infra.reset_hook_failures()
                self._fake_probes(simulation=simulation)
                run_cmd = MagicMock()
                self.assertEqual(infra._apt_install_missing(run_cmd, ["python3-new"], [], "t"), [])
                run_cmd.assert_not_called()
                self.assertEqual(infra.get_hook_failures()[0][0], "apt_install:t")

    def test_refuses_when_apt_itself_refuses_over_a_hold(self):
        self._fake_probes(sim_rc=100)
        run_cmd = MagicMock()
        self.assertEqual(infra._apt_install_missing(run_cmd, ["python3-new"], [], "t"), [])
        run_cmd.assert_not_called()

    def test_provision_environment_has_no_bare_apt_install_left(self):
        source = inspect.getsource(infra.provision_environment)
        self.assertNotIn('["apt-get", "install", "-y"]', source)
        self.assertEqual(source.count("_apt_install_missing("), 3)


class DeployedDaemonsCopyTests(_TmpDirTestCase):
    """Tests [@ANCHOR: infrastructure:_skip_deployed_copy]

    Round-2 production runbook, 2026-10-03: provisioning copied hams_com's src daemons/ over
    /opt/hams/daemons and so reverted 79 newer files deployed by `deploy_to_production.py
    --sync-daemons`, then crashed on a dangling symlink."""

    def _manifest(self, src, dest, flagged=True):
        spec = {
            "src": src, "path": dest, "owner": None, "mode": "755",
            "environments": ["prod"], "post_provision_hooks": [MagicMock(__name__="hook")],
        }
        if flagged:
            spec["deployed_by_sync_daemons"] = True
        self.safe_patch_dict(infra.MANIFEST, {"static_files": [spec]})
        return spec

    def safe_patch_dict(self, target, values):
        patcher = patch.dict(target, values)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _tree(self):
        src = os.path.join(self.tmp, "src")
        os.makedirs(src)
        with open(os.path.join(src, "main.py"), "w") as f:
            f.write("new = 1\n")
        dest = os.path.join(self.tmp, "daemons")
        return src, dest

    def test_a_deployed_host_keeps_its_daemons_and_runs_no_hooks(self):
        src, dest = self._tree()
        os.makedirs(dest)
        with open(os.path.join(dest, "main.py"), "w") as f:
            f.write("deployed = 2\n")
        deploy_log = os.path.join(self.tmp, "DEPLOY_LOG")
        open(deploy_log, "w").close()
        self.safe_patch_object(infra, "DEPLOY_LOG_PATH", deploy_log)
        spec = self._manifest(src, dest)
        infra.provision_static_files(MagicMock(), {}, environment="prod")
        with open(os.path.join(dest, "main.py")) as f:
            self.assertEqual(f.read(), "deployed = 2\n")
        spec["post_provision_hooks"][0].assert_not_called()

    def test_a_fresh_install_still_copies_and_skips_a_dangling_symlink(self):
        src, dest = self._tree()
        os.symlink("/nonexistent/hams_open/ham_digital_modes", os.path.join(src, "ham_digital_modes"))
        self.safe_patch_object(infra, "DEPLOY_LOG_PATH", os.path.join(self.tmp, "no_DEPLOY_LOG"))
        self.safe_patch_object(infra, "apply_permissions")
        spec = self._manifest(src, dest)
        infra.provision_static_files(MagicMock(), {}, environment="prod")
        with open(os.path.join(dest, "main.py")) as f:
            self.assertEqual(f.read(), "new = 1\n")
        spec["post_provision_hooks"][0].assert_called_once()
        self.assertEqual(infra.get_hook_failures(), [])

    def test_both_daemon_trees_are_flagged_in_the_manifest(self):
        flagged = {s["path"] for s in infra.MANIFEST["static_files"] if s.get("deployed_by_sync_daemons")}
        self.assertEqual(flagged, {"/opt/hams/daemons", "/opt/hams/daemons/backup_worker"})


class PreserveExistingDirectoryTests(_TmpDirTestCase):
    def test_an_existing_preserve_existing_directory_keeps_its_mode(self):
        # Tests [@ANCHOR: infrastructure:apply_production_directories]
        existing = os.path.join(self.tmp, "var/log/redis")
        os.makedirs(existing)
        os.chmod(existing, 0o2750)
        missing = os.path.join(self.tmp, "new")
        self.safe_patch_dict(infra.MANIFEST, {"directories": [
            {"path": existing, "owner": None, "provision_mode": "755", "environments": ["prod"],
             "preserve_existing": True},
            {"path": missing, "owner": None, "provision_mode": "750", "environments": ["prod"],
             "preserve_existing": True},
        ]})
        infra.apply_production_directories(environment="prod")
        self.assertEqual(os.stat(existing).st_mode & 0o7777, 0o2750)
        self.assertEqual(os.stat(missing).st_mode & 0o777, 0o750)

    def safe_patch_dict(self, target, values):
        patcher = patch.dict(target, values)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_var_log_redis_is_preserved(self):
        entry = [d for d in infra.MANIFEST["directories"] if d["path"] == "/var/log/redis"][0]
        self.assertTrue(entry.get("preserve_existing"))


class ConditionalRestartTests(_SafePatchTestCase):
    def test_redis_acl_write_reports_whether_anything_changed(self):
        self.safe_patch_object(infra, "apply_permissions")
        with tempfile.TemporaryDirectory() as directory:
            conf = os.path.join(directory, "redis.conf")
            include = os.path.join(directory, "hams-acl.conf")
            open(conf, "w").close()
            env_vars = {"REDIS_USERNAME": "hams_redis", "REDIS_PASSWORD": "pw"}
            self.assertTrue(infra._write_redis_acl_include(env_vars, conf, include))
            self.assertFalse(infra._write_redis_acl_include(env_vars, conf, include))
            env_vars["REDIS_PASSWORD"] = "pw2"
            self.assertTrue(infra._write_redis_acl_include(env_vars, conf, include))

    def test_each_restart_is_conditional_on_its_config_change(self):
        source = inspect.getsource(infra.provision_environment)
        for service, guard in (
            ("rabbitmq-server", "if rabbitmq_conf_changed:"),
            ("redis-server", "redis_conf_changed"),
            ("postgresql", "if postgresql_restart_needed:"),
        ):
            idx = source.index(f'["systemctl", "restart", "{service}"]')
            self.assertIn(guard, source[idx - 300:idx], service)


class ProvisionPlanModeTests(_TmpDirTestCase):
    """Tests [@ANCHOR: infrastructure:provision_plan]

    provision_environment(plan=True) runs end to end here with every mutating primitive patched to
    fail the test: nothing may be written, created, linked, chowned, copied or run."""

    READ_ONLY_PREFIXES = (
        "dpkg-query", "apt-mark showhold", "apt-get -s", "apt-cache", "bash -c apt-cache",
        "systemctl is-enabled", "rabbitmqctl list_users", "sudo -u postgres psql", "dpkg-architecture",
    )

    def _forbid(self, name):
        def fail(*args, **kwargs):
            raise AssertionError(f"plan mode called {name}{args}")
        return fail

    def test_plan_changes_nothing_and_prints_no_secret(self):
        repo = os.path.join(self.tmp, "hams_open")
        for rel in ("hams_shared/tools", "zero_sudo", "backup_management/daemon"):
            os.makedirs(os.path.join(repo, rel))
        open(os.path.join(repo, "zero_sudo", "__manifest__.py"), "w").close()
        hams_com = os.path.join(self.tmp, "hams_com")
        os.makedirs(os.path.join(hams_com, "ham_base"))
        os.makedirs(os.path.join(hams_com, "daemons"))
        open(os.path.join(hams_com, "ham_base", "__manifest__.py"), "w").close()
        pg_conf = os.path.join(self.tmp, "postgresql.conf")
        with open(pg_conf, "w") as f:
            f.write("listen_addresses = '*'\n")

        printed = []

        def fake_subprocess_run(cmd, *args, **kwargs):
            text = " ".join(cmd)
            if text.startswith("sudo -u postgres psql") and "SELECT" not in (kwargs.get("input") or ""):
                raise AssertionError(f"plan mode ran a mutating psql: {cmd}")
            if not text.startswith(self.READ_ONLY_PREFIXES):
                raise AssertionError(f"plan mode ran {cmd}")
            stdout = ""
            if text.startswith("systemctl is-enabled"):
                stdout = "disabled\n"
            elif text.startswith("apt-cache show"):
                stdout = f"Package: {cmd[-1]}\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

        real_open = builtins.open

        def guarded_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                raise AssertionError(f"plan mode opened {file!r} for writing")
            return real_open(file, mode, *args, **kwargs)

        real_os_open = os.open

        def guarded_os_open(path, flags, *args, **kwargs):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                raise AssertionError(f"plan mode os.open'ed {path!r} for writing")
            return real_os_open(path, flags, *args, **kwargs)

        self.safe_patch_object(infra.subprocess, "run", side_effect=fake_subprocess_run)
        self.safe_patch_object(builtins, "open", side_effect=guarded_open)
        self.safe_patch_object(infra.os, "open", side_effect=guarded_os_open)
        for name in ("symlink", "makedirs", "chown", "chmod", "replace", "remove", "rename", "fchmod"):
            self.safe_patch_object(infra.os, name, side_effect=self._forbid(f"os.{name}"))
        for name in ("copytree", "copy2", "rmtree"):
            self.safe_patch_object(infra.shutil, name, side_effect=self._forbid(f"shutil.{name}"))

        def fill_env(env, is_test):
            # Stands in for load_and_prompt_env (which reads the host's real /opt/hams/etc):
            # every placeholder the MANIFEST renders gets a harmless value.
            for spec in infra.MANIFEST["static_files"]:
                for field in ("content", "path", "src"):
                    for name in re.findall(r"\{([A-Z][A-Z0-9_]*)\}", spec.get(field, "") or ""):
                        env.setdefault(name, "placeholder")
            for keys in infra.MANIFEST["env_groups"].values():
                for key in keys:
                    env.setdefault(key, "placeholder")

        self.safe_patch_object(infra, "load_and_prompt_env", side_effect=fill_env)
        self.safe_patch_object(infra, "download_file", side_effect=self._forbid("download_file"))
        self.safe_patch_object(infra, "initialize_odoo_database", side_effect=self._forbid("initialize_odoo_database"))
        self.safe_patch_object(infra, "run_post_provision_smoketest", side_effect=self._forbid("smoketest"))
        original_lockdown = infra._apply_postgresql_lockdown
        self.safe_patch_object(infra, "_apply_postgresql_lockdown", side_effect=lambda: original_lockdown([pg_conf]))
        self.safe_patch_dict(os.environ, {"HAMS_ISOLATED_NS": ""})
        del os.environ["HAMS_ISOLATED_NS"]

        def plan_run(cmd, **kwargs):
            printed.append(" ".join(infra.redact_command(cmd)))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        env_vars = {
            "REPO_ROOT": repo, "DB_NAME": "hams_prod", "DB_PASS": "SEKRET-db",
            "REDIS_USERNAME": "hams_redis", "REDIS_PASSWORD": "SEKRET-redis",
            "RMQ_USER": "hams_rabbitmq", "RMQ_PASS": "SEKRET-rmq",
        }
        with patch("sys.stdout") as stdout:
            recorded = infra.provision_environment(
                plan_run, env_vars, None, os_id="debian", hold_odoo=True, plan=True
            )
        stdout_text = "".join(str(c.args[0]) for c in stdout.write.call_args_list if c.args)
        self.assertFalse(infra._planning(), "plan mode must be switched off afterwards")
        kinds = {kind for kind, _ in recorded.actions}
        self.assertTrue({"write", "hook"} <= kinds, kinds)
        self.assertTrue(any("restart postgresql" in cmd for cmd in printed), printed)
        self.assertTrue(any(cmd.startswith("apt-get install -y --no-upgrade") for cmd in printed), printed)
        everything = stdout_text + "\n".join(printed) + repr(recorded.actions)
        for secret in ("SEKRET-db", "SEKRET-redis", "SEKRET-rmq"):
            self.assertNotIn(secret, everything)
        with real_open(pg_conf) as f:
            self.assertEqual(f.read(), "listen_addresses = '*'\n")

    def safe_patch_dict(self, target, values):
        patcher = patch.dict(target, values)
        patcher.start()
        self.addCleanup(patcher.stop)


class TicketTriageTimerUnitTests(unittest.TestCase):
    """Bruce, 2026-10-04 (NIGHT_PLAN decision 199): the AI ticket-triage agent runs on a timer,
    drafts only. The unit must be linked but never enabled until the operator opts in, must never
    run in a test environment, and must carry the kill switch and a state directory."""

    UNITS = (
        "ticket.triage.service", "ticket.triage.timer", "ticket.triage.path", "ticket.triage.event.service",
    )

    def _spec(self, name):
        for spec in infra.MANIFEST["static_files"]:
            if os.path.basename(spec["path"]) == name:
                return spec
        self.fail(f"{name} missing from the MANIFEST")

    def test_both_units_are_external_fetch_and_opt_in_and_prod_only(self):
        for name in self.UNITS:
            spec = self._spec(name)
            self.assertTrue(spec.get("external_fetch"), name)
            self.assertTrue(spec.get("opt_in"), name)
            self.assertEqual(spec["environments"], ["prod"], name)
        self.assertLessEqual(set(self.UNITS), infra.opt_in_unit_names())
        self.assertLessEqual(set(self.UNITS), infra.external_fetch_unit_names())

    def test_the_timer_never_replays_missed_runs_and_spreads_its_start(self):
        text = self._spec("ticket.triage.timer")["content"]
        self.assertIn("Persistent=false", text)
        self.assertIn("RandomizedDelaySec=", text)
        # The timer is only the FALLBACK since triage became event-driven (NIGHT_PLAN decision 222):
        # every few hours, not hourly.
        self.assertIn("OnCalendar=*-*-* 00/4:07:00", text)
        self.assertNotIn("OnCalendar=hourly", text)

    def test_the_service_has_the_kill_switch_state_directory_and_a_timeout_above_the_cli_ceiling(self):
        text = self._spec("ticket.triage.service")["content"]
        self.assertIn("ConditionPathExists=!/opt/hams/etc/ticket_triage.disabled", text)
        self.assertIn("StateDirectory=hams-ticket-triage", text)
        timeout = int(re.search(r"^TimeoutStartSec=(\d+)$", text, re.M).group(1))
        self.assertGreater(timeout, 660)  # pre-check 60s + CLI hard timeout 600s
        self.assertIn("Type=oneshot", text)

    def test_the_path_unit_watches_the_spool_hams_helpdesk_writes_and_starts_the_event_service(self):
        text = self._spec("ticket.triage.path")["content"]
        self.assertIn("PathExistsGlob=/opt/hams/spool/ticket_triage/ticket-*.json", text)
        self.assertIn("Unit=ticket.triage.event.service", text)
        self.assertIn("WantedBy=paths.target", text)
        self.assertIn("TriggerLimitBurst=", text)

    def test_the_spool_directory_is_odoo_owned_private_and_prod_only(self):
        entries = [e for e in infra.MANIFEST["directories"] if e["path"] == "/opt/hams/spool/ticket_triage"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["owner"], "odoo:odoo")
        self.assertEqual(entries[0]["provision_mode"], "700")
        self.assertEqual(entries[0]["environments"], ["prod"])

    def test_the_event_service_consumes_before_anything_can_skip_or_fail_it(self):
        text = self._spec("ticket.triage.event.service")["content"]
        self.assertIn("ExecStart=/usr/bin/python3 /opt/hams/daemons/ticket_triage_agent/main.py --event", text)
        # A skipped or failed start would leave the wake-up files, and the path unit would re-fire
        # until its trigger limit: the kill switch lives in main.py, which deletes the files first.
        self.assertNotIn("ConditionPathExists", text)
        self.assertNotIn("ExecStartPre", text)
        self.assertIn("ReadWritePaths=/home/hams_ai_agent -/opt/hams/ai_triage_repo /opt/hams/spool/ticket_triage", text)
        self.assertIn("HAMS_TRIAGE_SPOOL_DIR=/opt/hams/spool/ticket_triage", text)
        self.assertIn("StateDirectory=hams-ticket-triage", text)  # the same lock and counters as the timer path
        self.assertIn("Type=oneshot", text)
        timeout = int(re.search(r"^TimeoutStartSec=(\d+)$", text, re.M).group(1))
        self.assertGreaterEqual(timeout, 120 + 700 + 60 + 600)  # debounce + lock wait + pre-check + CLI

    def test_the_event_service_has_the_same_sandbox_as_the_timer_service_apart_from_its_spool_path(self):
        def sandbox(name):
            text = self._spec(name)["content"]
            keep = ("ProtectSystem=", "ProtectHome=", "PrivateTmp=", "PrivateDevices=", "RestrictAddressFamilies=",
                    "CapabilityBoundingSet=", "User=", "StateDirectory=", "WorkingDirectory=")
            return {line for line in text.splitlines() if line.startswith(keep)}
        self.assertEqual(sandbox("ticket.triage.service"), sandbox("ticket.triage.event.service"))
        directives = [
            line for line in self._spec("ticket.triage.event.service")["content"].splitlines()
            if not line.startswith("#")
        ]
        self.assertFalse([line for line in directives if line.startswith("NoNewPrivileges")])

    def test_the_event_service_is_on_the_shared_odoo_account_ratchet(self):
        self.assertIn("ticket.triage.event.service", infra.SHARED_ODOO_ACCOUNT_UNITS)

    def test_provisioning_links_but_does_not_enable_the_path_unit_unless_named_and_never_in_test(self):
        linked = ["ticket.triage.path", "ticket.triage.timer", "stray.odoo.shell.detector.timer"]
        self.assertEqual(
            infra._activation_units_to_enable(linked, is_test_env=False), ["stray.odoo.shell.detector.timer"],
        )
        self.assertIn(
            "ticket.triage.path",
            infra._activation_units_to_enable(linked, False, opt_in_units=("ticket.triage.path",)),
        )
        self.assertNotIn(
            "ticket.triage.path",
            infra._activation_units_to_enable(linked, True, opt_in_units=("ticket.triage.path",)),
        )
        for name in ("ticket.triage.event.service", "ticket.triage.path"):
            self.assertIn(name, infra.opt_in_unit_names())
            self.assertIn(name, infra.external_fetch_unit_names())
        self.assertNotIn("ticket.triage.event.service", infra._smoketest_candidate_services(True, False))
        self.assertNotIn(
            "ticket.triage.event.service",
            infra._smoketest_candidate_services(True, True, opt_in_units=("ticket.triage.event.service",)),
        )

    def test_the_model_is_not_given_a_shell_account_the_unit_is_only_the_supervisor(self):
        accounts = {a["user"]: a for a in infra.MANIFEST["system_accounts"]}
        self.assertEqual(accounts["hams_ai_agent"]["shell"], "/usr/sbin/nologin")
        self.assertIn("ticket.triage.service", infra.SHARED_ODOO_ACCOUNT_UNITS)

    def test_provisioning_links_but_does_not_enable_the_timer_unless_named(self):
        linked = ["ticket.triage.timer", "stray.odoo.shell.detector.timer"]
        self.assertEqual(
            infra._activation_units_to_enable(linked, is_test_env=False),
            ["stray.odoo.shell.detector.timer"],
        )
        self.assertIn(
            "ticket.triage.timer",
            infra._activation_units_to_enable(linked, False, opt_in_units=("ticket.triage.timer",)),
        )
        self.assertNotIn(
            "ticket.triage.timer",
            infra._activation_units_to_enable(linked, True, opt_in_units=("ticket.triage.timer",)),
        )

    def test_the_smoketest_never_starts_the_service_unless_named_and_never_in_test(self):
        self.assertNotIn("ticket.triage.service", infra._smoketest_candidate_services(True, False))
        self.assertNotIn(
            "ticket.triage.service",
            infra._smoketest_candidate_services(True, True, opt_in_units=("ticket.triage.service",)),
        )

    def test_the_mcp_wrapper_refuses_every_argument_but_count_pending(self):
        text = self._spec("run-ticket-triage-mcp.sh")["content"]
        self.assertIn("--count-pending)", text)
        self.assertIn("exit 2", text)
        self.assertNotIn("export HAMS_TRIAGE_ENABLE_PROPOSE_FIX", text)


class ModelFilesTests(unittest.TestCase):
    """Tests [@ANCHOR: infrastructure:provision_model_files]

    The simulated-band bots' Piper voice and faster-whisper model are fetched on a production
    host only, checksum-verified. No test here touches the network: the transport is injected."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        infra.reset_hook_failures()

    def _manifest(self, contents):
        specs = []
        for name, data in contents.items():
            specs.append({
                "path": os.path.join(self.tmp, name),
                "url": f"https://example.invalid/{name}",
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "owner": "hams_com:hams_com",
                "mode": "640",
            })
        return patch.dict(infra.MANIFEST, {"model_files": specs})

    def _fetcher(self, served, calls):
        def fetch(url, part_path, expected_size):
            calls.append(url)
            with open(part_path, "wb") as f:
                f.write(served[url.rsplit("/", 1)[1]])
        return fetch

    def test_the_manifest_pins_every_file_and_has_a_directory_for_it(self):
        specs = infra.MANIFEST["model_files"]
        self.assertGreaterEqual(len(specs), 6)
        directories = {d["path"] for d in infra.MANIFEST["directories"]}
        seen = set()
        for spec in specs:
            self.assertRegex(spec["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(spec["size"], 0)
            self.assertTrue(spec["url"].startswith("https://"))
            self.assertTrue(spec["path"].startswith("/opt/hams/models/"))
            self.assertNotIn(spec["path"], seen)
            seen.add(spec["path"])
            self.assertIn(os.path.dirname(spec["path"]), directories)

    def test_the_bot_units_point_the_daemon_at_exactly_the_manifest_paths(self):
        paths = {s["path"] for s in infra.MANIFEST["model_files"]}
        for unit in ("hams.simulated.bots.service", "hams.simulated.observer.service"):
            spec = next(s for s in infra.MANIFEST["static_files"] if s["path"].endswith("/" + unit))
            self.assertIn('Environment="HAMS_PIPER_VOICE_PATH=/opt/hams/models/piper/en_US-lessac-low.onnx"', spec["content"])
            self.assertIn('Environment="HAMS_WHISPER_MODEL_DIR=/opt/hams/models/faster-whisper-tiny.en"', spec["content"])
        self.assertIn("/opt/hams/models/piper/en_US-lessac-low.onnx", paths)
        for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
            self.assertIn("/opt/hams/models/faster-whisper-tiny.en/" + name, paths)

    def test_a_test_environment_never_fetches(self):
        calls = []
        with self._manifest({"a.bin": b"abc"}):
            self.assertEqual(infra.provision_model_files(environment="test", fetch=self._fetcher({"a.bin": b"abc"}, calls)), [])
            with patch.dict(os.environ, {"HAMS_ISOLATED_NS": "1"}):
                self.assertEqual(infra.provision_model_files(environment="prod", fetch=self._fetcher({"a.bin": b"abc"}, calls)), [])
            self.assertEqual(infra.provision_model_files(environment="prod", dest_dir=self.tmp, fetch=self._fetcher({"a.bin": b"abc"}, calls)), [])
        self.assertEqual(calls, [])

    def test_the_default_transport_is_never_reached_by_a_test_environment(self):
        with self._manifest({"a.bin": b"abc"}), patch.object(infra, "_fetch_model_to") as real:
            infra.provision_model_files(environment="test")
        real.assert_not_called()

    def test_a_missing_file_is_fetched_verified_and_installed(self):
        calls = []
        data = {"a.bin": b"abc" * 100}
        with self._manifest(data), patch.object(infra, "apply_permissions"):
            failed = infra.provision_model_files(environment="prod", fetch=self._fetcher(data, calls))
        self.assertEqual(failed, [])
        self.assertEqual(len(calls), 1)
        with open(os.path.join(self.tmp, "a.bin"), "rb") as f:
            self.assertEqual(f.read(), data["a.bin"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "a.bin.part")))

    def test_a_file_that_already_verifies_is_not_fetched_again(self):
        calls = []
        data = {"a.bin": b"hello"}
        with open(os.path.join(self.tmp, "a.bin"), "wb") as f:
            f.write(data["a.bin"])
        with self._manifest(data), patch.object(infra, "apply_permissions"):
            self.assertEqual(infra.provision_model_files(environment="prod", fetch=self._fetcher(data, calls)), [])
        self.assertEqual(calls, [])

    def test_a_download_that_fails_the_checksum_is_not_installed_and_is_recorded(self):
        calls = []
        wrong = {"a.bin": b"WRONG"}
        with self._manifest({"a.bin": b"right"}), patch.object(infra, "apply_permissions"):
            failed = infra.provision_model_files(environment="prod", fetch=self._fetcher(wrong, calls))
        self.assertEqual(failed, [os.path.join(self.tmp, "a.bin")])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "a.bin")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "a.bin.part")))
        self.assertTrue(any(name.startswith("model_files:") for name, _ in infra.get_hook_failures()))

    def test_a_bad_download_never_replaces_an_installed_file(self):
        installed = os.path.join(self.tmp, "a.bin")
        with open(installed, "wb") as f:
            f.write(b"OLD")  # does not verify, so a refetch is attempted
        calls = []
        with self._manifest({"a.bin": b"right"}), patch.object(infra, "apply_permissions"):
            failed = infra.provision_model_files(environment="prod", fetch=self._fetcher({"a.bin": b"bad!!"}, calls))
        self.assertEqual(failed, [installed])
        with open(installed, "rb") as f:
            self.assertEqual(f.read(), b"OLD")

    def test_a_transport_error_is_recorded_not_raised(self):
        def broken(url, part_path, expected_size):
            raise OSError("network down")
        with self._manifest({"a.bin": b"x"}), patch.object(infra, "apply_permissions"):
            failed = infra.provision_model_files(environment="prod", fetch=broken)
        self.assertEqual(len(failed), 1)

    def test_plan_mode_downloads_nothing(self):
        calls = []
        with self._manifest({"a.bin": b"x"}), patch("builtins.print"), infra.planning():
            infra.provision_model_files(environment="prod", fetch=self._fetcher({"a.bin": b"x"}, calls))
        self.assertEqual(calls, [])


class EventAiEnrichmentTimerUnitTests(unittest.TestCase):
    """NIGHT_PLAN 203 (Bruce, 2026-10-04): the AI correction of scraped hamfest listings runs on a timer next to
    the automatic club crawl. Linked but never enabled until the operator opts in, never run in a test
    environment, with a kill switch, a state directory for its ledger, and the credential.touch.timer
    exception (it reaches the CLI through sudo)."""

    UNITS = ("event.ai.enrichment.service", "event.ai.enrichment.timer")

    def _spec(self, name):
        for spec in infra.MANIFEST["static_files"]:
            if os.path.basename(spec["path"]) == name:
                return spec
        self.fail(f"{name} missing from the MANIFEST")

    def test_both_units_are_external_fetch_opt_in_and_prod_only(self):
        for name in self.UNITS:
            spec = self._spec(name)
            self.assertTrue(spec.get("external_fetch"), name)
            self.assertTrue(spec.get("opt_in"), name)
            self.assertEqual(spec["environments"], ["prod"], name)
        self.assertLessEqual(set(self.UNITS), infra.opt_in_unit_names())
        self.assertLessEqual(set(self.UNITS), infra.external_fetch_unit_names())

    def test_provisioning_links_but_enables_and_starts_only_when_the_operator_names_the_unit(self):
        timer = "event.ai.enrichment.timer"
        linked = [timer, "stray.odoo.shell.detector.timer"]
        self.assertNotIn(timer, infra._activation_units_to_enable(linked, False))
        self.assertNotIn(timer, infra._activation_units_to_enable(linked, True))
        self.assertIn(timer, infra._activation_units_to_enable(linked, False, opt_in_units=(timer,)))
        self.assertNotIn(timer, infra._activation_units_to_enable(linked, True, opt_in_units=(timer,)))
        service = "event.ai.enrichment.service"
        self.assertNotIn(service, infra._smoketest_candidate_services(True, False))
        self.assertNotIn(service, infra._smoketest_candidate_services(False, False))

    def test_the_timer_never_replays_missed_runs_and_spreads_its_start(self):
        text = self._spec("event.ai.enrichment.timer")["content"]
        self.assertIn("Persistent=false", text)
        self.assertIn("RandomizedDelaySec=", text)
        self.assertIn("OnCalendar=*-*-* 00/3:40:00", text)
        self.assertNotIn("OnCalendar=hourly", text)

    def test_the_service_has_the_kill_switch_state_directory_caps_and_a_smoketest(self):
        text = self._spec("event.ai.enrichment.service")["content"]
        self.assertIn("ConditionPathExists=!/opt/hams/etc/event_ai_enrichment.disabled", text)
        self.assertIn("StateDirectory=hams-event-ai-enrichment", text)
        self.assertIn("EVENT_AI_ENRICHMENT_STATE_DIR=/var/lib/hams-event-ai-enrichment", text)
        self.assertIn("EVENT_AI_ENRICHMENT_MAX_EVENTS_PER_DAY=", text)
        self.assertIn("ExecStartPre=/usr/bin/python3 /opt/hams/daemons/event_ai_enrichment/main.py --start-test", text)
        self.assertIn("ExecStart=/usr/bin/python3 /opt/hams/daemons/event_ai_enrichment/main.py --scheduled", text)
        self.assertIn("Type=oneshot", text)
        self.assertIn("ODOO_KEY_FILE=/opt/hams/etc/keys/event_ai_correction_service_internal.key", text)
        timeout = int(re.search(r"^TimeoutStartSec=(\d+)h$", text, re.M).group(1))
        self.assertGreaterEqual(timeout, 1)

    def test_the_service_has_the_agent_sudo_shape_and_no_credential_beyond_common_env(self):
        text = self._spec("event.ai.enrichment.service")["content"]
        self.assertNotRegex(text, r"(?m)^NoNewPrivileges=")  # the sudo hop to hams_event_agent needs a setuid binary
        self.assertIn("CapabilityBoundingSet=CAP_SETUID CAP_SETGID", text)
        self.assertIn("ReadWritePaths=/home/hams_event_agent\n", text)
        env_files = re.findall(r"^EnvironmentFile=-?(\S+)$", text, re.M)
        self.assertEqual(env_files, ["/opt/hams/etc/common.env"])
        self.assertIn("event.ai.enrichment.service", infra.SHARED_ODOO_ACCOUNT_UNITS)

    def test_the_odoo_grant_to_the_agent_wrapper_it_uses_exists(self):
        grants = [
            spec for spec in infra.MANIFEST["static_files"]
            if spec["path"] == "/etc/sudoers.d/odoo-event-agent-claude-sandbox"
        ]
        self.assertEqual(len(grants), 1)
        self.assertIn("odoo ALL=(hams_event_agent) NOPASSWD: /usr/local/sbin/run-hams-event-agent-claude.sh", grants[0]["content"])


if __name__ == "__main__":
    unittest.main()


class EtcHostsTemplateTests(_SafePatchTestCase):
    """The /etc/hosts template must keep the machine's own name resolvable (2026-10-03)."""

    def _hosts_entry(self):
        entries = [e for e in infra.MANIFEST["static_files"] if e.get("path") == "/etc/hosts"]
        self.assertEqual(len(entries), 1)
        return entries[0]

    def test_template_has_a_hostname_line(self):
        self.assertIn("127.0.1.1 {LOCAL_HOSTNAME}", self._hosts_entry()["content"])

    def test_local_hostname_is_filled_from_the_machine(self):
        self.safe_patch("infrastructure.socket.gethostname", return_value="testbox")
        env_vars = {}
        infra._ensure_local_hostname(self._hosts_entry()["content"], env_vars)
        rendered = infra.format_env(self._hosts_entry()["content"], env_vars)
        self.assertIn("127.0.1.1 testbox\n", rendered)

    def test_entries_without_the_placeholder_are_untouched(self):
        env_vars = {}
        infra._ensure_local_hostname("no placeholder here", env_vars)
        self.assertEqual(env_vars, {})


class FirewallRulesProvisioningTests(_TmpDirTestCase):
    """MANIFEST["firewall_rules"]: stun.hams.com's UDP 3478 on production only, added only to an
    active ufw, only when missing, never on a test host. subprocess.run (`ufw status`) is mocked."""

    ACTIVE = "Status: active\n\nTo   Action   From\n--   ------   ----\n51820/udp   ALLOW   Anywhere\n"
    WITH_STUN = ACTIVE + "3478/udp   ALLOW   Anywhere\n3478/udp (v6)   ALLOW   Anywhere (v6)\n"

    def _ufw(self, status_text, installed=True):
        self.safe_patch("infrastructure.shutil.which", return_value="/usr/sbin/ufw" if installed else None)
        return self.safe_patch(
            "infrastructure.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout=status_text, stderr=""),
        )

    def test_the_manifest_opens_only_stun_udp_and_only_on_production(self):
        # Tests [@ANCHOR: infrastructure:provision_firewall_rules]
        rules = infra.MANIFEST["firewall_rules"]
        self.assertEqual([r["args"] for r in rules], [["allow", "3478/udp"]])
        self.assertEqual(rules[0]["environments"], ["prod"])

    def test_adds_the_rule_to_an_active_ufw_that_lacks_it(self):
        # Tests [@ANCHOR: infrastructure:provision_firewall_rules]
        self._ufw(self.ACTIVE)
        run = MagicMock()
        infra.provision_firewall_rules(run, environment="prod")
        run.assert_called_once_with(
            ["ufw", "allow", "3478/udp", "comment", "stun.hams.com (hams_relay_bridge STUN responder)"]
        )

    def test_a_rule_already_present_is_left_alone(self):
        # Tests [@ANCHOR: infrastructure:provision_firewall_rules]
        self._ufw(self.WITH_STUN)
        run = MagicMock()
        infra.provision_firewall_rules(run, environment="prod")
        run.assert_not_called()

    def test_a_test_host_never_gets_a_public_port(self):
        # Tests [@ANCHOR: infrastructure:provision_firewall_rules]
        probe = self._ufw(self.ACTIVE)
        run = MagicMock()
        infra.provision_firewall_rules(run, environment="test")
        run.assert_not_called()
        probe.assert_not_called()

    def test_no_ufw_or_an_inactive_one_is_not_touched(self):
        # Tests [@ANCHOR: infrastructure:provision_firewall_rules]
        run = MagicMock()
        self._ufw("", installed=False)
        infra.provision_firewall_rules(run, environment="prod")
        self._ufw("Status: inactive\n")
        infra.provision_firewall_rules(run, environment="prod")
        run.assert_not_called()

    def test_plan_mode_reports_the_rule_and_runs_nothing(self):
        # Tests [@ANCHOR: infrastructure:provision_firewall_rules]
        self._ufw(self.ACTIVE)
        run = MagicMock()
        with infra.planning() as plan:
            infra.provision_firewall_rules(run, environment="prod")
        run.assert_not_called()
        self.assertEqual([k for k, _ in plan.actions], ["firewall"])
        self.assertIn("3478/udp", plan.actions[0][1])

    def test_a_failing_ufw_is_a_hook_failure_not_a_crash(self):
        # Tests [@ANCHOR: infrastructure:provision_firewall_rules]
        self._ufw(self.ACTIVE)
        run = MagicMock(side_effect=subprocess.CalledProcessError(1, ["ufw"]))
        infra.provision_firewall_rules(run, environment="prod")
        self.assertEqual([n for n, _ in infra.get_hook_failures()], ["firewall_rules"])

    def test_production_gets_a_stun_bind_default_and_a_test_host_does_not(self):
        # Tests [@ANCHOR: infrastructure:provision_firewall_rules]
        prod = {"DOMAIN": "hams.com"}
        infra.load_and_prompt_env(prod, is_test=False)
        self.assertEqual(prod["BRIDGE_STUN_BIND"], "[::]:3478")
        test = {}
        infra.load_and_prompt_env(test, is_test=True)
        self.assertNotIn("BRIDGE_STUN_BIND", test)
        # An operator's value, including an empty one that switches the responder off, is kept.
        for value in ("0.0.0.0:3478", ""):
            custom = {"DOMAIN": "hams.com", "BRIDGE_STUN_BIND": value}
            infra.load_and_prompt_env(custom, is_test=False)
            self.assertEqual(custom["BRIDGE_STUN_BIND"], value)

    def test_the_bind_default_is_written_to_bridge_env(self):
        self.assertIn("BRIDGE_STUN_BIND", infra.MANIFEST["env_groups"]["bridge.env"])


class ShackConsoleUnitTests(unittest.TestCase):
    """The shack console's unit as provisioned (hams_com daemons/shack_console/packaging/shack-console.service is identical)."""

    def _unit(self):
        entry = next(i for i in infra.MANIFEST["static_files"] if i.get("path") == "/opt/hams/systemd/shack-console.service")
        return entry["content"]

    def test_the_dynamic_user_can_reach_the_binary_under_opt_hams(self):
        # hams1's /opt/hams is mode 750 with an execute-only ACL for hams_traverse; DynamicUser cannot exec without it.
        self.assertIn("\nSupplementaryGroups=hams_traverse\n", self._unit())

    def test_it_stays_loopback_only_and_dynamic(self):
        unit = self._unit()
        for line in ("DynamicUser=yes", "IPAddressDeny=any", "IPAddressAllow=localhost"):
            self.assertIn(line, unit)


class AuthGatewayDirectoriesTests(unittest.TestCase):
    """The auth.hams.com gateway (hams_com daemons/hams_auth_gateway) reads its pinned trust anchors from one
    directory per anchor under /etc/hams/auth/anchors. Its config pins files in `arrl_lotw` and `hams_member`;
    a directory missing here makes install_config.py (hams_com) fail at release time, so both are ratcheted."""

    ANCHOR_DIRECTORIES = (
        "/etc/hams/auth/anchors/arrl_lotw",
        "/etc/hams/auth/anchors/hams_member",
    )

    def _directories(self):
        return {d["path"]: d for d in infra.MANIFEST["directories"]}

    def test_the_configuration_and_both_anchor_directories_exist(self):
        directories = self._directories()
        for path in ("/etc/hams/auth",) + self.ANCHOR_DIRECTORIES:
            self.assertIn(path, directories)

    def test_they_are_production_only_group_hams_auth_and_never_mounted_elsewhere(self):
        directories = self._directories()
        for path in ("/etc/hams/auth",) + self.ANCHOR_DIRECTORIES:
            spec = directories[path]
            self.assertEqual(spec["owner"], "root:hams-auth", path)
            self.assertEqual(spec["provision_mode"], "750", path)
            self.assertEqual(spec["environments"], ["prod"], path)
            self.assertNotIn("runtime_mount", spec, path)

    def test_the_account_and_unit_are_still_provisioned(self):
        users = {u["user"]: u for u in infra.MANIFEST["system_accounts"]}
        self.assertIn("hams-auth", users)
        self.assertEqual(users["hams-auth"]["shell"], "/usr/sbin/nologin")
        self.assertEqual(users["hams-auth"]["environments"], ["prod"])
        paths = {f["path"] for f in infra.MANIFEST["static_files"]}
        self.assertIn("/opt/hams/systemd/hams-auth-gateway.service", paths)

    def test_the_account_can_reach_its_binary_under_opt_hams_but_is_not_in_hams_com(self):
        # /opt/hams is 0750 hams_com with an execute-only ACL for hams_traverse; the unit's ExecStart is under it.
        accounts = infra.MANIFEST["system_accounts"]
        names = [a["user"] for a in accounts]
        account = accounts[names.index("hams-auth")]
        self.assertEqual(account["member_of"], ["hams_traverse"])
        self.assertEqual(account["not_member_of"], ["hams_com"])
        self.assertLess(names.index("hams_traverse"), names.index("hams-auth"))
        unit = next(f for f in infra.MANIFEST["static_files"] if f["path"].endswith("/hams-auth-gateway.service"))
        self.assertIn("ExecStart=/opt/hams/daemons/", unit["content"])
        dirs = {d["path"]: d for d in infra.MANIFEST["directories"]}
        self.assertEqual(dirs["/opt/hams"]["acl"], ["g:hams_traverse:--x"])


class NginxFrontEndManifestTests(_TmpDirTestCase):
    """Tests [@ANCHOR: infrastructure:hook_enable_nginx_front_end]

    Production's loopback buffering proxy (127.0.0.1:8085, the Cloudflare tunnel's catch-all) was
    installed by hand on 2026-10-05 because Odoo's prefork workers cut a send that stalls for 2
    seconds. These tests make a fresh provision reproduce it, and keep Debian's port-80 default
    site and the hand-made acme-only site from coming back."""

    def _entry(self):
        return next(
            e for e in infra.MANIFEST["static_files"]
            if e["path"] == "/etc/nginx/hams/hams-tunnel-origin.conf"
        )

    def test_the_site_is_copied_from_hams_com_in_production_only(self):
        entry = self._entry()
        self.assertEqual(entry["src"], "{HAMS_COM_DIR}/nginx/prod/hams-tunnel-origin.conf")
        self.assertEqual(entry["environments"], ["prod"])
        self.assertEqual(entry["owner"], "root:root")
        self.assertIn(infra.hook_enable_nginx_front_end, entry["post_provision_hooks"])

    def test_nginx_package_is_still_installed(self):
        self.assertTrue(any(p["name"] == "nginx" for p in infra.MANIFEST["apt_packages"]))

    def test_no_manifest_entry_writes_an_acme_only_or_port_80_site(self):
        for entry in infra.MANIFEST["static_files"]:
            path = entry["path"]
            if path.startswith("/etc/nginx/"):
                self.assertNotIn("acme-only", path)
                self.assertNotIn("sites-available", path)
                self.assertNotIn("listen 80", entry.get("content", ""))
        self.assertIn("default", infra.NGINX_SITES_TO_DISABLE)
        self.assertIn("acme-only.conf", infra.NGINX_SITES_TO_DISABLE)

    def _stage(self):
        sites = os.path.join(self.tmp, "sites-enabled")
        os.makedirs(sites)
        for name in ("default", "acme-only.conf"):
            os.symlink("/nonexistent", os.path.join(sites, name))
        conf = os.path.join(self.tmp, "hams-tunnel-origin.conf")
        with open(conf, "w") as f:
            f.write("server { listen 127.0.0.1:8085; }\n")
        self.safe_patch_object(infra, "NGINX_SITES_ENABLED", sites)
        return sites, conf

    def test_hook_links_the_site_removes_port_80_sites_then_tests_before_enabling(self):
        sites, conf = self._stage()
        run = MagicMock()
        infra.hook_enable_nginx_front_end({}, "", conf, run)
        self.assertEqual(sorted(os.listdir(sites)), ["hams-tunnel-origin.conf"])
        self.assertEqual(
            os.readlink(os.path.join(sites, "hams-tunnel-origin.conf")), infra.NGINX_TUNNEL_ORIGIN_CONF
        )
        self.assertEqual(
            [c.args[0] for c in run.call_args_list],
            [
                ["/usr/sbin/nginx", "-t"],
                ["systemctl", "enable", "nginx.service"],
                ["systemctl", "reload-or-restart", "nginx.service"],
            ],
        )

    def test_hook_does_not_enable_nginx_when_the_config_test_fails(self):
        self._stage()
        conf = os.path.join(self.tmp, "hams-tunnel-origin.conf")
        run = MagicMock(side_effect=subprocess.CalledProcessError(1, "nginx -t"))
        infra.hook_enable_nginx_front_end({}, "", conf, run)
        self.assertEqual(run.call_count, 1)
        self.assertEqual([n for n, _ in infra.get_hook_failures()], ["hook_enable_nginx_front_end"])

    def test_hook_is_idempotent(self):
        sites, conf = self._stage()
        run = MagicMock()
        infra.hook_enable_nginx_front_end({}, "", conf, run)
        infra.hook_enable_nginx_front_end({}, "", conf, run)
        self.assertEqual(os.listdir(sites), ["hams-tunnel-origin.conf"])
        self.assertEqual(infra.get_hook_failures(), [])

    def test_hook_does_nothing_when_the_site_file_was_not_installed(self):
        sites, _ = self._stage()
        run = MagicMock()
        infra.hook_enable_nginx_front_end({}, "", os.path.join(self.tmp, "missing.conf"), run)
        run.assert_not_called()
        self.assertEqual(len(os.listdir(sites)), 2)

    def test_a_staged_tree_is_only_linked_never_started(self):
        stage = os.path.join(self.tmp, "stage")
        conf = os.path.join(stage, "etc/nginx/hams/hams-tunnel-origin.conf")
        os.makedirs(os.path.dirname(conf))
        with open(conf, "w") as f:
            f.write("x\n")
        run = MagicMock()
        infra.hook_enable_nginx_front_end({}, stage, conf, run)
        run.assert_not_called()
        self.assertTrue(os.path.islink(os.path.join(stage, "etc/nginx/sites-enabled/hams-tunnel-origin.conf")))
