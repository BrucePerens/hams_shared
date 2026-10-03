#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for tenant_lib, tenant_ctl and tenant_cloudflare.

The provisioning code is run for real against a FakeSystem: file operations happen in a temporary
directory (so permissions, content and idempotence are real), and only the commands that need root
or a PostgreSQL server (useradd, psql, systemctl, nft, pg_dump) are answered by a small in-memory
model.
"""

# [@ANCHOR: test_tenant_lib:validate_spec]
# Tests [@ANCHOR: tenant_lib:validate_spec]
# [@ANCHOR: test_tenant_lib:render_odoo_conf]
# Tests [@ANCHOR: tenant_lib:render_odoo_conf]
# [@ANCHOR: test_tenant_lib:render_unit_dropin]
# Tests [@ANCHOR: tenant_lib:render_unit_dropin]
# [@ANCHOR: test_tenant_lib:render_pg_hba]
# Tests [@ANCHOR: tenant_lib:render_pg_hba]
# [@ANCHOR: test_tenant_lib:render_nft]
# Tests [@ANCHOR: tenant_lib:render_nft]
# [@ANCHOR: test_tenant_lib:render_pg_sql]
# Tests [@ANCHOR: tenant_lib:render_pg_sql]
# [@ANCHOR: test_tenant_lib:build_create_steps]
# Tests [@ANCHOR: tenant_lib:build_create_steps]
# [@ANCHOR: test_tenant_lib:backup_tenant]
# Tests [@ANCHOR: tenant_lib:backup_tenant]
# [@ANCHOR: test_tenant_lib:restore_test]
# Tests [@ANCHOR: tenant_lib:restore_test]
# [@ANCHOR: test_tenant_lib:status]
# Tests [@ANCHOR: tenant_lib:status]
# [@ANCHOR: test_tenant_lib:upgrade_tenant]
# Tests [@ANCHOR: tenant_lib:upgrade_tenant]
# [@ANCHOR: test_tenant_lib:delete_tenant]
# Tests [@ANCHOR: tenant_lib:delete_tenant]
# [@ANCHOR: test_tenant_cloudflare:build_ingress]
# Tests [@ANCHOR: tenant_cloudflare:build_ingress]

import copy
import datetime
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import infrastructure as infra
import tenant_cloudflare as cf
import tenant_ctl
import tenant_lib as lib

HERE = os.path.dirname(os.path.abspath(__file__))
PERENS = {
    "name": "perens_com",
    "domains": ["Perens.com", "www.perens.com"],
    "http_port": 18101,
    "modules": ["website"],
}
PARKING = {"name": "parking", "http_port": 18110, "catch_all": True, "extra_modules": ["parking"]}


class FakeSystem(lib.System):
    """Real file operations in a temp tree; commands answered by an in-memory model."""

    def __init__(self, root):
        self.root = root
        self.commands = []
        self.stdin = []
        self.users = set()
        self.roles = set()
        self.databases = {}  # name -> {"modules": set, "extensions": set, "revoked": bool}
        self.role_limits = {}
        self.active = set()
        self.hba_file = os.path.join(root, "pg", "pg_hba.conf")
        self.hba_errors = "0"
        self.clock = datetime.datetime(2026, 10, 4, 3, 0, tzinfo=datetime.timezone.utc)
        self.dump_ok = True
        os.makedirs(os.path.dirname(self.hba_file), exist_ok=True)
        with open(self.hba_file, "w", encoding="utf-8") as handle:
            handle.write(
                "local   all   postgres   peer\nlocal   all   all   peer\n"
                "host    all   all   127.0.0.1/32   scram-sha-256\n"
            )

    def _chown(self, path, owner):
        self.commands.append(["chown", owner, path])

    def now(self):
        return self.clock

    def user_exists(self, user):
        return user in self.users

    def mutating(self):
        """Commands that change something: everything except chown noise, SELECT/SHOW and is-active."""
        found = []
        for command in self.commands:
            text = " ".join(command)
            if command[0] == "chown" or command[:2] == ["systemctl", "is-active"]:
                continue
            if "psql" in command and ("-c" in command) and re.search(r"-c (SELECT [^p]|SHOW)", text):
                if "pg_reload_conf" not in text:
                    continue
            found.append(command)
        return found

    def run_to_file(self, argv, path):
        self.commands.append(argv)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(b"PGDMP-fake-dump")

    def run_from_file(self, argv, path):
        assert os.path.exists(path), path
        self.run(argv)

    def run(self, argv, input_text=None, check=True):
        self.commands.append(list(argv))
        if input_text is not None:
            self.stdin.append(input_text)
        result = self._answer(list(argv), input_text)
        if check and result.returncode != 0:
            raise RuntimeError(f"fake command failed: {argv}")
        return result

    def _answer(self, argv, input_text):
        if argv[0] == "useradd":
            self.users.add(argv[-1])
            return lib.Result(0)
        if argv[0] == "userdel":
            self.users.discard(argv[-1])
            return lib.Result(0)
        if argv[0] in ("nft",):
            return lib.Result(0)
        if argv[0] == "systemctl":
            return self._systemctl(argv)
        if argv[:3] == ["runuser", "-u", "postgres"] and "psql" in argv:
            return self._psql(argv, input_text)
        if argv[:3] == ["runuser", "-u", "postgres"] and "pg_restore" in argv:
            scratch = argv[argv.index("-d") + 1]
            self.databases[scratch] = {"modules": {"base"}, "extensions": set(), "revoked": False}
            return lib.Result(0)
        if argv[0] == "runuser" and "--config" in argv:
            name = argv[argv.index("-d") + 1]
            flag = "-i" if "-i" in argv else "-u"
            wanted = argv[argv.index(flag) + 1].split(",")
            if flag == "-i":
                self.databases[name]["modules"].update(wanted)
            return lib.Result(0)
        raise AssertionError(f"unexpected command {argv}")

    def _systemctl(self, argv):
        unit = argv[-1]
        if argv[1] == "is-active":
            return lib.Result(0 if unit in self.active else 3, "active\n" if unit in self.active else "inactive\n")
        if argv[1] == "enable":
            self.active.add(unit)
        if argv[1] == "disable":
            self.active.discard(unit)
        return lib.Result(0)

    def _psql(self, argv, input_text):
        db = argv[argv.index("-d") + 1]
        sql = argv[argv.index("-c") + 1] if "-c" in argv else input_text
        if sql.startswith("SHOW hba_file"):
            return lib.Result(0, self.hba_file + "\n")
        if "pg_hba_file_rules" in sql:
            return lib.Result(0, self.hba_errors + "\n")
        if sql.startswith("SELECT pg_reload_conf"):
            return lib.Result(0, "t\n")
        match = re.search(r"FROM pg_roles WHERE rolname = '(\w+)'", sql)
        if sql.startswith("SELECT 1 FROM pg_roles") and match:
            return lib.Result(0, "1\n" if match.group(1) in self.roles else "\n")
        if sql.startswith("SELECT rolconnlimit"):
            role = re.search(r"rolname = '(\w+)'", sql).group(1)
            return lib.Result(0, f"{self.role_limits.get(role, '')}\n")
        if sql.startswith("CREATE ROLE"):
            role = sql.split()[2]
            self.roles.add(role)
            limit = re.search(r"CONNECTION LIMIT (\d+)", sql)
            self.role_limits[role] = int(limit.group(1)) if limit else -1
            return lib.Result(0)
        if sql.startswith("ALTER ROLE"):
            role = sql.split()[2]
            self.role_limits[role] = int(re.search(r"LIMIT (\d+)", sql).group(1))
            return lib.Result(0)
        if sql.startswith("SELECT 1 FROM pg_database"):
            name = re.search(r"datname = '(\w+)'", sql).group(1)
            return lib.Result(0, "1\n" if name in self.databases else "\n")
        if sql.startswith("CREATE DATABASE"):
            name = sql.split()[2]
            self.databases[name] = {"modules": set(), "extensions": set(), "revoked": False}
            return lib.Result(0)
        if sql.startswith("DROP DATABASE"):
            self.databases.pop(re.search(r"DROP DATABASE (?:IF EXISTS )?(\w+)", sql).group(1), None)
            return lib.Result(0)
        if sql.startswith("DROP ROLE"):
            self.roles.discard(sql.split()[-1].rstrip(";"))
            return lib.Result(0)
        if sql.startswith("REVOKE ALL ON DATABASE"):
            self.databases[sql.split()[4]]["revoked"] = True
            return lib.Result(0)
        if sql.startswith("SELECT (d.datacl"):
            name = re.search(r"datname = '(\w+)'", sql).group(1)
            return lib.Result(0, "1\n" if self.databases[name]["revoked"] else "0\n")
        if sql.startswith("CREATE EXTENSION"):
            self.databases[db]["extensions"].update({"pg_trgm", "unaccent"})
            return lib.Result(0)
        if "FROM pg_extension" in sql:
            return lib.Result(0, f"{len(self.databases[db]['extensions'])}\n")
        if "FROM ir_module_module" in sql and "string_agg" in sql:
            if db not in self.databases or not self.databases[db]["modules"]:
                return lib.Result(1, stderr="relation does not exist")
            return lib.Result(0, ",".join(sorted(self.databases[db]["modules"])) + "\n")
        if "count(*) FROM ir_module_module" in sql:
            return lib.Result(0, f"{len(self.databases[db]['modules'])}\n")
        if "count(*) FROM res_users" in sql:
            return lib.Result(0, "1\n")
        if "pg_database_size" in sql:
            return lib.Result(0, "12345678\n")
        if input_text and "UPDATE res_users" in input_text:
            return lib.Result(0)
        raise AssertionError(f"unexpected SQL {sql!r}")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tenant_lib_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        t = self.tmp
        self.paths = lib.Paths(
            conf_root=f"{t}/etc/hams-tenants", secret_root=f"{t}/opt/etc/tenants",
            spec_dir=f"{t}/opt/etc/tenants.d", data_root=f"{t}/var/lib/hams-tenants",
            backup_root=f"{t}/opt/backups/tenants", addons_root=f"{t}/usr/local/addons",
            systemd_dir=f"{t}/etc/systemd/system", nft_file=f"{t}/etc/hams-tenants/firewall.nft",
            hams_open_src=f"{t}/src/hams_open",
        )
        os.makedirs(f"{t}/src/hams_open/parking/models")
        with open(f"{t}/src/hams_open/parking/__manifest__.py", "w", encoding="utf-8") as handle:
            handle.write("{}")
        self.system = FakeSystem(t)
        self.spec = lib.validate_spec(PERENS)
        patcher = mock.patch.object(lib, "hash_password", lambda password, rounds=0: "HASH(" + password[:3] + ")")
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, _kind, filename):
        with open(os.path.join(self.paths.secret_dir("perens_com"), filename), encoding="utf-8") as handle:
            return handle.read().strip()

    def run_steps(self, spec, apply=True, **kw):
        out = io.StringIO()
        steps = lib.build_create_steps(spec, self.paths, self.system, **kw)
        lib.execute(steps, apply, lambda line: out.write(line + "\n"))
        return out.getvalue()


class SpecValidationTests(unittest.TestCase):
    def test_a_good_spec_gets_defaults_and_punycode(self):
        spec = lib.validate_spec(dict(PERENS, domains=["Bücher.example", "perens.com"]))
        self.assertEqual(spec["domains"], ["xn--bcher-kva.example", "perens.com"])
        self.assertEqual(spec["workers"], 0)
        self.assertEqual(spec["db_maxconn"], 10)

    def test_bad_names_are_refused(self):
        for name in ("Perens", "1abc", "hams_x", "postgres", "t_x", "a", "x" * 30, "pg_x", "template1"):
            with self.assertRaises(lib.SpecError, msg=name):
                lib.validate_spec(dict(PERENS, name=name))

    def test_bad_domains_are_refused(self):
        for domain in ("hams.com", "www.hams.com", "x.hams.com", "*.example.com", "example.com.",
                       "localhost", "10.0.0.1", "a b.example", "http://example.com", "-x.example.com"):
            with self.assertRaises(lib.SpecError, msg=domain):
                lib.validate_spec(dict(PERENS, domains=[domain]))

    def test_ports_must_be_in_the_tenant_range(self):
        for port in (8069, 8072, 80, 17999, 19000, "18101", True):
            with self.assertRaises(lib.SpecError, msg=port):
                lib.validate_spec(dict(PERENS, http_port=port))

    def test_workers_need_an_explicit_gevent_port_and_zero_workers_forbid_one(self):
        with self.assertRaises(lib.SpecError):
            lib.validate_spec(dict(PERENS, workers=2))
        with self.assertRaises(lib.SpecError):
            lib.validate_spec(dict(PERENS, workers=0, gevent_port=18102))
        ok = lib.validate_spec(dict(PERENS, workers=2, gevent_port=18102))
        self.assertEqual(ok["gevent_port"], 18102)

    def test_modules_outside_the_allowlist_are_refused(self):
        for module in ("ham_base", "cloudflare", "zero_sudo"):
            with self.assertRaises(lib.SpecError, msg=module):
                lib.validate_spec(dict(PERENS, extra_modules=[module]))
        lib.validate_spec(dict(PERENS, extra_modules=["parking"]))

    def test_unknown_keys_and_a_connection_limit_below_the_pool_are_refused(self):
        with self.assertRaises(lib.SpecError):
            lib.validate_spec(dict(PERENS, db_password="x"))
        with self.assertRaises(lib.SpecError):
            lib.validate_spec(dict(PERENS, db_maxconn=10, pg_connection_limit=11))

    def test_fleet_conflicts_are_found(self):
        one = lib.validate_spec(PERENS)
        other = lib.validate_spec(dict(PERENS, name="other_site"))
        with self.assertRaises(lib.SpecError):
            lib.validate_fleet([one, other])  # same domains and port
        two = lib.validate_spec(dict(PERENS, name="other_site", domains=["x.example"], http_port=18105))
        lib.validate_fleet([one, two])
        park1 = lib.validate_spec(PARKING)
        park2 = lib.validate_spec(dict(PARKING, name="parking2", http_port=18111))
        with self.assertRaises(lib.SpecError):
            lib.validate_fleet([park1, park2])

    def test_the_shipped_example_specs_are_valid_together(self):
        specs = lib.load_specs(os.path.join(HERE, "tenants"))
        self.assertEqual({s["name"] for s in specs}, {"perens_com", "postopen_org", "parking"})


class RenderTests(_Base):
    def test_conf_has_no_database_password_and_binds_loopback_only(self):
        text = lib.render_odoo_conf(self.spec, self.paths, "$pbkdf2-sha512$x")
        self.assertIn("http_interface = 127.0.0.1", text)
        self.assertIn("dbfilter = ^perens_com$", text)
        self.assertIn("list_db = False", text)
        self.assertIn("db_user = t_perens_com", text)
        self.assertNotIn("db_password", text.replace("No db_password", ""))
        self.assertIn("workers = 0", text)
        self.assertNotIn("gevent_port", text)
        self.assertIn("admin_passwd = $pbkdf2-sha512$x", text)
        self.assertNotIn("8069", text)
        self.assertNotIn("8072", text)

    def test_conf_with_workers_sets_limits_and_its_own_gevent_port(self):
        spec = lib.validate_spec(dict(PERENS, workers=2, gevent_port=18103))
        text = lib.render_odoo_conf(spec, self.paths, "h")
        self.assertIn("gevent_port = 18103", text)
        self.assertIn("limit_memory_hard = ", text)
        self.assertIn("workers = 2", text)

    def test_extra_modules_add_only_the_tenants_own_addons_directory(self):
        spec = lib.validate_spec(dict(PERENS, extra_modules=["parking"]))
        text = lib.render_odoo_conf(spec, self.paths, "h")
        self.assertIn(f"addons_path = {self.paths.stock_addons},{self.paths.addons_dir('perens_com')}", text)
        self.assertNotIn("/opt/hams", text)

    def test_unit_dropin_carries_the_limits_and_exact_ports(self):
        text = lib.render_unit_dropin(lib.validate_spec(dict(PERENS, workers=1, gevent_port=18104)))
        for needle in ("MemoryMax=768M", "MemorySwapMax=0", "CPUQuota=100%", "TasksMax=256",
                       "SocketBindAllow=tcp:18101", "SocketBindAllow=tcp:18104"):
            self.assertIn(needle, text)

    def test_pg_hba_allows_exactly_one_role_on_one_database_by_peer(self):
        self.assertIn("local   postgres,perens_com   t_perens_com   peer", lib.render_pg_hba(self.spec))
        self.assertIn("local   all   +hams_tenant   reject", lib.PG_HBA_REJECT)
        self.assertIn("host    all   +hams_tenant   0.0.0.0/0   reject", lib.PG_HBA_REJECT)

    def test_nft_lists_every_tenant_and_blocks_local_services(self):
        text = lib.render_nft([self.spec, lib.validate_spec(PARKING)])
        self.assertIn('"t_parking", "t_perens_com"', text)
        self.assertIn('oifname "lo" reject', text)
        self.assertIn("ct state established,related accept", text)
        self.assertIn("169.254.0.0/16", text)
        self.assertIn("10.99.0.0/24", text)
        self.assertIn("delete table inet hams_tenants", lib.render_nft([]))
        self.assertNotIn("skuid", lib.render_nft([]))

    def test_the_nft_text_is_accepted_by_nft_when_it_is_installed(self):
        nft = shutil.which("nft")
        if not nft:
            self.skipTest("nft not installed")
        import subprocess

        path = os.path.join(self.tmp, "fw.nft")
        # nft resolves user names at load time, so check the syntax with root, which always exists.
        text = lib.render_nft([lib.validate_spec(dict(PERENS, name="root_x"))]).replace("t_root_x", "root")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        proc = subprocess.run(["sudo", "-n", nft, "-c", "-f", path], capture_output=True, text=True)
        if "password" in proc.stderr or "not permitted" in proc.stderr:
            self.skipTest("no root for nft -c")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_set_admin_sql_escapes_quotes(self):
        sql = lib.render_set_admin_sql("o'brien", "$h", "https://x.example")
        self.assertIn("'o''brien'", sql)
        self.assertIn("web.base.url.freeze", sql)

    def test_the_template_unit_has_the_isolation_the_design_promises(self):
        unit = next(s for s in infra.MANIFEST["static_files"] if s["path"].endswith("hams-tenant@.service"))
        content = unit["content"]
        for needle in ("User=t_%i", "ProtectSystem=strict", "ReadWritePaths=/var/lib/hams-tenants/%i",
                       "NoNewPrivileges=true", "ProtectProc=invisible", "InaccessiblePaths=/opt/hams",
                       "SocketBindDeny=any", "CapabilityBoundingSet=", "Restart=on-failure"):
            self.assertIn(needle, content)
        self.assertEqual(unit["host_class"], "odoo_tenants")
        self.assertNotIn("EnvironmentFile", content)  # no hams secret reaches a tenant
        self.assertNotIn("/etc/odoo/odoo.conf", content)


class CreateStepsTests(_Base):
    def test_plan_changes_nothing(self):
        out = self.run_steps(self.spec, apply=False)
        self.assertIn("todo os_user", out)
        self.assertIn("todo install_modules", out)
        self.assertEqual(self.system.mutating(), [])
        self.assertFalse(os.path.exists(self.paths.conf_dir("perens_com")))
        self.assertFalse(self.system.users)
        self.assertFalse(self.system.roles)

    def test_apply_creates_everything_with_the_right_modes(self):
        self.run_steps(self.spec)
        self.assertIn("t_perens_com", self.system.users)
        self.assertIn("perens_com", self.system.databases)
        self.assertEqual(self.system.databases["perens_com"]["modules"], {"base", "website"})
        self.assertTrue(self.system.databases["perens_com"]["revoked"])
        self.assertEqual(self.system.role_limits["t_perens_com"], 14)
        conf = self.paths.conf_file("perens_com")
        self.assertEqual(oct(os.stat(conf).st_mode & 0o777), "0o640")
        secret = os.path.join(self.paths.secret_dir("perens_com"), "admin_password")
        self.assertEqual(oct(os.stat(secret).st_mode & 0o777), "0o600")
        self.assertIn(["chown", "root:t_perens_com", conf + ".tmp"], self.system.commands)
        self.assertIn("hams-tenant@perens_com.service", self.system.active)
        with open(self.system.hba_file, encoding="utf-8") as handle:
            hba = handle.read()
        self.assertLess(hba.index("include_dir"), hba.index("local   all   all   peer"))
        hba_dir = os.path.join(os.path.dirname(self.system.hba_file), "hams-tenants.d")
        self.assertTrue(os.path.exists(os.path.join(hba_dir, "10-perens_com.conf")))
        self.assertTrue(os.path.exists(os.path.join(hba_dir, lib.PG_HBA_REJECT_FILE)))
        self.assertTrue(os.path.exists(self.paths.nft_file))

    def test_a_second_run_does_nothing(self):
        self.run_steps(self.spec)
        self.system.commands.clear()
        out = self.run_steps(self.spec)
        self.assertNotIn("run  ", out)
        self.assertEqual(self.system.mutating(), [])
        mutations = [c for c in self.system.commands if c[0] in ("useradd", "systemctl")
                     and c[1] not in ("is-active",)]
        self.assertEqual(mutations, [])
        self.assertEqual(self.system.stdin, self.system.stdin[:1])  # admin SQL only the first time

    def test_the_admin_password_never_appears_in_any_command_line_or_output(self):
        out = self.run_steps(self.spec)
        secret = self.read("secret", "admin_password")
        master = self.read("secret", "master_password")
        self.assertNotEqual(secret, master)
        for password in (secret, master):
            self.assertNotIn(password, out)
            for command in self.system.commands:
                self.assertNotIn(password, " ".join(command))
            with open(self.paths.conf_file("perens_com"), encoding="utf-8") as handle:
                self.assertNotIn(password, handle.read())

    def test_the_initial_install_runs_as_the_tenant_without_http_or_cron(self):
        self.run_steps(self.spec)
        install = next(c for c in self.system.commands if "--config" in c)
        self.assertEqual(install[:4], ["runuser", "-u", "t_perens_com", "--"])
        for flag in ("--stop-after-init", "--no-http", "--max-cron-threads=0", "--without-demo=True"):
            self.assertIn(flag, install)
        self.assertEqual(install[install.index("-i") + 1], "base,website")

    def test_a_broken_pg_hba_is_rolled_back_and_nothing_is_reloaded(self):
        self.system.hba_errors = "2"
        with self.assertRaises(RuntimeError):
            self.run_steps(self.spec)
        hba_dir = os.path.join(os.path.dirname(self.system.hba_file), "hams-tenants.d")
        self.assertFalse(os.path.exists(os.path.join(hba_dir, "10-perens_com.conf")))
        reloads = [c for c in self.system.commands if any("pg_reload_conf" in str(a) for a in c)]
        self.assertEqual(reloads, [])

    def test_extra_modules_are_copied_root_owned_and_installed(self):
        spec = lib.validate_spec(PARKING)
        self.run_steps(spec)
        copied = os.path.join(self.paths.addons_dir("parking"), "parking", "__manifest__.py")
        self.assertTrue(os.path.exists(copied))
        self.assertEqual(oct(os.stat(copied).st_mode & 0o777), "0o644")
        self.assertEqual(self.system.databases["parking"]["modules"], {"base", "parking"})

    def test_no_firewall_and_no_start_flags(self):
        out = self.run_steps(self.spec, apply_firewall=False, start=False)
        self.assertNotIn("firewall", out)
        self.assertNotIn("service:", out)

    def test_hba_include_insertion_refuses_a_file_it_cannot_place(self):
        with self.assertRaises(RuntimeError):
            lib.insert_pg_hba_include("host all all 0.0.0.0/0 md5\n", "include_dir x")


class BackupAndStatusTests(_Base):
    def setUp(self):
        super().setUp()
        self.run_steps(self.spec)
        store = os.path.join(self.paths.data_dir("perens_com"), "filestore", "perens_com", "ab")
        os.makedirs(store)
        with open(os.path.join(store, "abcdef"), "w", encoding="utf-8") as handle:
            handle.write("attachment")

    def test_backup_writes_checksummed_files_and_prunes(self):
        for hour in range(10):
            self.system.clock = datetime.datetime(2026, 10, 4, hour, 0, tzinfo=datetime.timezone.utc)
            directory = lib.backup_tenant(self.spec, self.paths, self.system, keep=3)
        self.assertEqual(lib.verify_backup(directory), [])
        self.assertEqual(len(os.listdir(self.paths.backup_dir("perens_com"))), 3)
        self.assertEqual(oct(os.stat(os.path.join(directory, "db.dump")).st_mode & 0o777), "0o600")

    def test_a_tampered_backup_is_detected(self):
        directory = lib.backup_tenant(self.spec, self.paths, self.system)
        with open(os.path.join(directory, "db.dump"), "ab") as handle:
            handle.write(b"x")
        self.assertIn("db.dump checksum mismatch", lib.verify_backup(directory))
        with self.assertRaises(RuntimeError):
            lib.restore_test(self.spec, self.paths, self.system)

    def test_restore_test_restores_into_a_scratch_database_and_drops_it(self):
        lib.backup_tenant(self.spec, self.paths, self.system)
        result = lib.restore_test(self.spec, self.paths, self.system)
        self.assertEqual(result["users"], 1)
        self.assertGreaterEqual(result["filestore_members"], 3)
        self.assertNotIn("restoretest_perens_com", self.system.databases)

    def test_restore_test_without_a_backup_fails(self):
        with self.assertRaises(RuntimeError):
            lib.restore_test(self.spec, self.paths, self.system)

    def test_status_reports_unit_sizes_backup_age_and_probes(self):
        lib.backup_tenant(self.spec, self.paths, self.system)
        self.system.clock += datetime.timedelta(hours=5)
        row = lib.tenant_status(self.spec, self.paths, self.system, lambda port, host: 200)
        self.assertEqual(row["unit"], "active")
        self.assertEqual(row["backup_age_hours"], 5.0)
        self.assertEqual(row["http"], {"perens.com": 200, "www.perens.com": 200})
        self.assertTrue(row["healthy"])
        self.assertGreater(row["filestore_bytes"], 0)
        bad = lib.tenant_status(self.spec, self.paths, self.system, lambda port, host: 502)
        self.assertFalse(bad["healthy"])
        self.system.active.clear()
        self.assertFalse(lib.tenant_status(self.spec, self.paths, self.system)["healthy"])


class DeleteTests(_Base):
    def setUp(self):
        super().setUp()
        self.run_steps(self.spec)

    def test_delete_backs_up_first_and_keeps_the_backup(self):
        steps = lib.build_delete_steps(self.spec, self.paths, self.system, all_specs=[self.spec])
        self.assertEqual(steps[0].key, "final_backup")
        lib.execute(steps, True, lambda line: None)
        self.assertNotIn("perens_com", self.system.databases)
        self.assertNotIn("t_perens_com", self.system.roles)
        self.assertNotIn("t_perens_com", self.system.users)
        self.assertFalse(os.path.exists(self.paths.conf_dir("perens_com")))
        self.assertEqual(len(os.listdir(self.paths.backup_dir("perens_com"))), 1)

    def test_the_cli_refuses_without_the_exact_confirmation(self):
        args = ["delete", "perens_com", "--confirm-delete", "wrong"]
        with self.assertRaises(SystemExit) as raised:
            tenant_ctl.main(args, self.system, self.paths)
        self.assertIn("refusing", str(raised.exception))
        self.assertIn("perens_com", self.system.databases)
        with self.assertRaises(SystemExit):
            tenant_ctl.main(["delete", "perens_com"], self.system, self.paths)


class UpgradeTests(_Base):
    def setUp(self):
        super().setUp()
        self.run_steps(self.spec)

    def test_upgrade_backs_up_stops_upgrades_and_starts_in_that_order(self):
        steps = lib.build_upgrade_steps(self.spec, self.paths, self.system)
        self.assertEqual([s.key for s in steps], ["backup", "stop", "upgrade", "start"])
        self.system.commands.clear()
        lib.execute(steps, True, lambda line: None)
        flat = [" ".join(c) for c in self.system.commands if c[0] != "chown"]
        stop = next(i for i, c in enumerate(flat) if "systemctl stop" in c)
        upgrade = next(i for i, c in enumerate(flat) if "-u base,website" in c)
        start = next(i for i, c in enumerate(flat) if "systemctl start" in c)
        dump = next(i for i, c in enumerate(flat) if "pg_dump" in c)
        self.assertLess(dump, stop)
        self.assertLess(stop, upgrade)
        self.assertLess(upgrade, start)
        self.assertTrue(any("--stop-after-init" in c and "--no-http" in c for c in flat))

    def test_cli_upgrade_is_a_plan_by_default(self):
        stdout = io.StringIO()
        self.system.commands.clear()
        with mock.patch("sys.stdout", stdout):
            tenant_ctl.main(["upgrade", "perens_com"], self.system, self.paths)
        self.assertIn("PLAN (nothing is changed", stdout.getvalue())
        self.assertFalse([c for c in self.system.commands if "--stop-after-init" in c and "-u" in c])


class CtlTests(_Base):
    def spec_file(self, spec):
        path = os.path.join(self.tmp, f"{spec['name']}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(spec, handle)
        return path

    def test_create_is_a_plan_by_default(self):
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            code = tenant_ctl.main(["create", self.spec_file(PERENS)], self.system, self.paths)
        self.assertEqual(code, 0)
        self.assertIn("PLAN (nothing is changed", stdout.getvalue())
        self.assertFalse(self.system.users)

    def test_apply_refuses_when_not_root_or_not_the_right_host_class(self):
        with mock.patch("os.geteuid", return_value=1000):
            with self.assertRaises(SystemExit):
                tenant_ctl.main(["create", self.spec_file(PERENS), "--apply"], self.system, self.paths)
        with mock.patch("os.geteuid", return_value=0), mock.patch.object(infra, "host_classes", return_value=set()):
            with self.assertRaises(SystemExit) as raised:
                tenant_ctl.main(["create", self.spec_file(PERENS), "--apply"], self.system, self.paths)
        self.assertIn("odoo_tenants", str(raised.exception))
        self.assertFalse(self.system.users)

    def test_apply_proceeds_on_a_designated_host(self):
        stdout = io.StringIO()
        with mock.patch("os.geteuid", return_value=0), mock.patch.object(
            infra, "host_classes", return_value={"odoo_tenants"}
        ), mock.patch("sys.stdout", stdout):
            tenant_ctl.main(["create", self.spec_file(PERENS), "--apply"], self.system, self.paths)
        self.assertIn("perens_com", self.system.databases)
        self.assertIn("not printed", stdout.getvalue())

    def test_a_conflicting_second_tenant_is_refused_before_anything_runs(self):
        self.run_steps(self.spec)
        clash = dict(PERENS, name="other_site")
        with self.assertRaises(lib.SpecError):
            tenant_ctl.main(["create", self.spec_file(clash)], self.system, self.paths)

    def test_backup_is_dry_run_without_apply_and_health_flags_stale_backups(self):
        self.run_steps(self.spec)
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            tenant_ctl.main(["backup", "--all"], self.system, self.paths)
        self.assertIn("would back up perens_com", stdout.getvalue())
        self.assertFalse(os.path.exists(self.paths.backup_dir("perens_com")) and os.listdir(self.paths.backup_dir("perens_com")))
        with mock.patch("sys.stdout", io.StringIO()), mock.patch.object(tenant_ctl, "http_probe", return_value=200):
            self.assertEqual(tenant_ctl.main(["health", "--all"], self.system, self.paths), 1)
            with mock.patch("os.geteuid", return_value=0), mock.patch.object(infra, "host_classes", return_value={"odoo_tenants"}):
                tenant_ctl.main(["backup", "--all", "--apply"], self.system, self.paths)
            self.assertEqual(tenant_ctl.main(["health", "--all"], self.system, self.paths), 0)

    def test_validate_command(self):
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            tenant_ctl.main(["validate", self.spec_file(PERENS)], self.system, self.paths)
        self.assertIn("ok perens_com", stdout.getvalue())


LIVE = [
    {"path": "^/websocket$", "service": "http://localhost:8072"},
    {"path": "^/ws/firehose$", "service": "http://localhost:8765"},
    {"hostname": "relay.hams.com", "path": "^/ws/daemon_uplink$", "service": "ws://localhost:8766"},
    {"path": "^/ws$", "service": "http://localhost:3000"},
    {"service": "http://localhost:8069"},
]


class CloudflarePlanTests(unittest.TestCase):
    def setUp(self):
        self.specs = [lib.validate_spec(PERENS), lib.validate_spec(PARKING)]

    def test_tenant_rules_come_first_and_hams_paths_are_scoped(self):
        new = cf.build_ingress(self.specs, copy.deepcopy(LIVE))
        self.assertEqual(new[0], {"hostname": "perens.com", "service": "http://localhost:18101"})
        self.assertEqual(new[1]["hostname"], "www.perens.com")
        for rule in new:
            if rule.get("path") and "hostname" not in rule:
                self.fail(f"a hostname-less path rule survived: {rule}")
        scoped = [r for r in new if r.get("path") == "^/websocket$"]
        self.assertEqual([r["hostname"] for r in scoped], ["hams.com", "*.hams.com"])
        self.assertIn({"hostname": "hams.com", "service": "http://localhost:8069"}, new)
        self.assertIn({"hostname": "*.hams.com", "service": "http://localhost:8069"}, new)
        self.assertEqual(new[-1], {"service": "http://localhost:18110"})

    def test_without_a_parking_tenant_the_catch_all_is_a_404(self):
        new = cf.build_ingress([self.specs[0]], copy.deepcopy(LIVE))
        self.assertEqual(new[-1], {"service": "http_status:404"})

    def test_building_from_its_own_output_changes_nothing(self):
        once = cf.build_ingress(self.specs, copy.deepcopy(LIVE))
        twice = cf.build_ingress(self.specs, copy.deepcopy(once))
        self.assertEqual(once, twice)
        self.assertEqual(cf.ingress_digest(once), cf.ingress_digest(twice))

    def test_the_input_is_never_mutated_and_a_list_without_catch_all_is_refused(self):
        live = copy.deepcopy(LIVE)
        cf.build_ingress(self.specs, live)
        self.assertEqual(live, LIVE)
        with self.assertRaises(ValueError):
            cf.build_ingress(self.specs, LIVE[:-1])
        with self.assertRaises(ValueError):
            cf.build_ingress(self.specs, [])

    def test_existing_hostname_rules_keep_their_order_and_content(self):
        new = cf.build_ingress(self.specs, copy.deepcopy(LIVE))
        relay = [r for r in new if r.get("hostname") == "relay.hams.com"]
        self.assertEqual(relay, [LIVE[2]])

    def test_dns_records_are_proxied_cnames_to_the_tunnel_and_skip_the_parking_tenant(self):
        records = cf.render_dns(self.specs, "abc-123")
        self.assertEqual(
            [(r["name"], r["content"], r["proxied"]) for r in records],
            [("perens.com", "abc-123.cfargotunnel.com", True), ("www.perens.com", "abc-123.cfargotunnel.com", True)],
        )

    def test_odoo_rows_view_of_the_plan_names_the_catch_all_the_module_cannot_set(self):
        new = cf.build_ingress(self.specs, copy.deepcopy(LIVE))
        rows, catch_all = cf.odoo_route_rows(new)
        self.assertEqual(catch_all, "http://localhost:18110")
        self.assertEqual(rows[0], {"sequence": 10, "hostname": "perens.com", "path": "", "service_url": "http://localhost:18101"})
        self.assertEqual(len(rows), len(new) - 1)
        text = cf.render_plan_text(self.specs, {"config": {"ingress": copy.deepcopy(LIVE)}}, "t", odoo_rows=True)
        self.assertIn("http://localhost:8069", text)
        self.assertIn("service_url=http://localhost:18101", text)

    def test_zone_candidates(self):
        self.assertEqual(cf.zone_candidates("www.example.co.uk"), ["www.example.co.uk", "example.co.uk", "co.uk"])

    def test_plan_text_prints_a_digest_and_no_secret(self):
        text = cf.render_plan_text(self.specs, {"config": {"ingress": copy.deepcopy(LIVE)}}, "tid")
        self.assertIn("sha256", text)
        self.assertIn("tid.cfargotunnel.com", text)
        self.assertNotIn("Bearer", text)


class FakeApiOpener:
    def __init__(self, live):
        self.live = live
        self.calls = []

    def __call__(self, request, timeout=0):
        self.calls.append((request.get_method(), request.full_url))
        if request.get_method() == "PUT":
            self.put = json.loads(request.data.decode())
        body = json.dumps({"success": True, "result": self.live}).encode()
        return io.BytesIO(body) if False else _Resp(body)


class _Resp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class CloudflareApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cf_apply_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.specs = [lib.validate_spec(PERENS), lib.validate_spec(PARKING)]
        self.live = {"version": 8, "config": {"ingress": copy.deepcopy(LIVE), "warp-routing": {"enabled": False}}}
        self.live_file = os.path.join(self.tmp, "live.json")
        with open(self.live_file, "w", encoding="utf-8") as handle:
            json.dump(self.live, handle)
        self.opener = FakeApiOpener(copy.deepcopy(self.live))
        self.api = cf.Api("TOKEN-NEVER-PRINTED", self.opener)
        self.out = []

    def apply(self, apply, sha):
        return cf.apply_ingress(self.api, "acct", "tun", self.specs, self.live_file, sha, apply,
                                os.path.join(self.tmp, "bk"), self.out.append)

    def digest(self):
        return cf.ingress_digest(cf.build_ingress(self.specs, copy.deepcopy(LIVE)))

    def test_dry_run_does_not_put(self):
        self.apply(False, None)
        self.assertEqual([m for m, _ in self.opener.calls], ["GET"])

    def test_apply_requires_the_approved_digest(self):
        with self.assertRaises(SystemExit):
            self.apply(True, "0" * 64)
        self.assertNotIn("PUT", [m for m, _ in self.opener.calls])

    def test_apply_puts_the_planned_list_keeps_other_config_and_saves_a_backup(self):
        self.apply(True, self.digest())
        self.assertEqual(self.opener.put["config"]["ingress"], cf.build_ingress(self.specs, copy.deepcopy(LIVE)))
        self.assertEqual(self.opener.put["config"]["warp-routing"], {"enabled": False})
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "bk", "tunnel-tun-v8.json")))
        self.assertNotIn("TOKEN-NEVER-PRINTED", "\n".join(self.out))

    def test_apply_refuses_when_the_live_configuration_moved_on(self):
        self.opener.live["version"] = 9
        with self.assertRaises(SystemExit):
            self.apply(True, self.digest())
        self.assertNotIn("PUT", [m for m, _ in self.opener.calls])

    def test_dns_conflicts_are_reported_and_never_modified(self):
        responses = {
            "/zones?name=perens.com": [{"id": "z1"}],
            "/zones/z1/dns_records?name=perens.com": [{"type": "A", "content": "1.2.3.4"}],
            "/zones/z1/dns_records?name=www.perens.com": [],
        }
        out = []

        class Api:
            def call(self, method, path, body=None):
                if method == "POST":
                    out.append(("POST", path))
                    return {}
                return responses.get(path, [])

        code = cf.plan_or_apply_dns(Api(), self.specs, "tid", True, out.append)
        self.assertEqual(code, 2)
        self.assertEqual([o for o in out if isinstance(o, tuple)], [("POST", "/zones/z1/dns_records")])
        self.assertTrue(any("CONFLICT" in str(o) for o in out))

    def test_credentials_file_is_parsed_and_missing_keys_fail(self):
        path = os.path.join(self.tmp, "env")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# c\nCLOUDFLARE_API_TOKEN=abc\nCLOUDFLARE_ACCOUNT_ID='x1'\nOTHER=1\n")
        creds = cf.read_credentials(path)
        self.assertEqual(creds["CLOUDFLARE_ACCOUNT_ID"], "x1")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("CLOUDFLARE_API_TOKEN=abc\n")
        with self.assertRaises(SystemExit):
            cf.read_credentials(path)


class ManifestTests(unittest.TestCase):
    def test_tenant_units_are_all_host_class_gated(self):
        names = [os.path.basename(s["path"]) for s in infra.MANIFEST["static_files"]
                 if os.path.basename(s["path"]).startswith("hams-tenant")]
        self.assertEqual(len(names), 8)
        for spec in infra.MANIFEST["static_files"]:
            if os.path.basename(spec["path"]).startswith("hams-tenant"):
                self.assertEqual(spec["host_class"], "odoo_tenants")
                self.assertNotIn("external_fetch", spec)

    def test_the_health_unit_is_a_root_oneshot_that_fails_when_a_tenant_is_unhealthy(self):
        unit = next(s for s in infra.MANIFEST["static_files"] if s["path"].endswith("hams-tenant-health.service"))
        self.assertIn("tenant_ctl.py health --all", unit["content"])
        self.assertIn("Type=oneshot", unit["content"])
        self.assertNotIn("User=", unit["content"])  # root: the backup directory is 0700 root

    def test_tenant_directories_are_host_class_gated_and_root_owned(self):
        wanted = {"/etc/hams-tenants", "/opt/hams/etc/tenants", "/opt/hams/etc/tenants.d",
                  "/var/lib/hams-tenants", "/opt/hams/backups/tenants", "/usr/local/lib/hams-tenant-addons"}
        found = {d["path"]: d for d in infra.MANIFEST["directories"] if d["path"] in wanted}
        self.assertEqual(set(found), wanted)
        for entry in found.values():
            self.assertEqual(entry["host_class"], "odoo_tenants")
            self.assertEqual(entry["owner"], "root:root")
        self.assertEqual(found["/opt/hams/etc/tenants"]["provision_mode"], "700")

    def test_hams1_style_hosts_do_not_get_tenant_entries_without_the_class(self):
        self.assertFalse(infra._in_host_class({"host_class": "odoo_tenants"}, classes=set()))
        self.assertTrue(infra._in_host_class({"host_class": "odoo_tenants"}, classes={"odoo_tenants"}))
        self.assertIn("odoo_tenants", infra.KNOWN_HOST_CLASSES)

    def test_no_hams_secret_environment_file_reaches_a_tenant_unit(self):
        unit = next(s for s in infra.MANIFEST["static_files"] if s["path"].endswith("hams-tenant@.service"))
        for secret_env in ("redis.env", "rabbitmq.env", "db.env", "core.env", "odoo.env", "smtp.env"):
            self.assertNotIn(secret_env, unit["content"])


if __name__ == "__main__":
    unittest.main()
