#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for static_site_ctl.py: the real step list against a fake system in a temp tree."""

# [@ANCHOR: test_static_site_ctl:render_config]
# Tests [@ANCHOR: static_site_ctl:render_config]
# [@ANCHOR: test_static_site_ctl:filtered_files]
# Tests [@ANCHOR: static_site_ctl:filtered_files]

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import infrastructure as infra
import static_site_ctl as ctl
import static_site_server as srv
import tenant_lib as lib

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = os.path.join(HERE, "tenants", "perens_com.json")


class FakeSystem(lib.System):
    def __init__(self):
        self.commands = []
        self.users = set()
        self.active = set()

    def _chown(self, path, owner):
        self.commands.append(["chown", owner, path])

    def user_exists(self, user):
        return user in self.users

    def run(self, argv, input_text=None, check=True):
        self.commands.append(list(argv))
        if argv[0] == "useradd":
            self.users.add(argv[-1])
        if argv[:2] == ["systemctl", "enable"]:
            self.active.add(argv[-1])
        if argv[:2] == ["systemctl", "is-active"]:
            return lib.Result(0 if argv[-1] in self.active else 3, "active\n" if argv[-1] in self.active else "inactive\n")
        return lib.Result(0)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="static-ctl-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.paths = ctl.StaticPaths(os.path.join(self.tmp, "var"), os.path.join(self.tmp, "etc"), os.path.join(self.tmp, "lib"),
                                     os.path.join(self.tmp, "systemd"))
        self.system = FakeSystem()
        self.spec = ctl.load_static_spec(SPEC)

    def mutating(self):
        return [c for c in self.system.commands if c[:2] != ["systemctl", "is-active"]]

    def tree(self):
        source = os.path.join(self.tmp, "src")
        files = {"static/a.pdf": b"pdf", "static/dir/b.html": b"<p>", "static/vid.webm": b"v", "static/.env": b"X=1",
                 "static/p4g/.git/config": b"[core]", "static/wp-config.php": b"<?php", "static/dump.sql": b"sql",
                 "static/Notes.log": b"log", "static/dir/id_rsa": b"key"}
        for rel, data in files.items():
            path = os.path.join(source, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(data)
        os.symlink("/etc/passwd", os.path.join(source, "static", "pw"))
        return source


class CreateTests(Base):
    def test_config_carries_the_port_and_prefix_from_the_tenant_spec_and_binds_loopback(self):
        config = json.loads(ctl.render_config(self.spec, self.paths))
        self.assertEqual((config["port"], config["prefix"], config["bind"]), (18201, "/static/", "127.0.0.1"))
        self.assertEqual(config["root"], os.path.join(self.paths.data_root, "perens_com"))
        self.assertEqual(ctl.render_dropin(self.spec).splitlines()[-1], "SocketBindAllow=tcp:18201")

    def test_a_spec_without_a_static_site_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"name": "plain_site", "domains": ["plain.example"], "http_port": 18150}, handle)
            with self.assertRaises(lib.SpecError):
                ctl.load_static_spec(path)

    def test_plan_changes_nothing_and_apply_is_idempotent(self):
        steps = ctl.build_create_steps(self.spec, self.paths, self.system)
        lines = []
        todo = lib.execute(steps, False, lines.append)
        self.assertEqual(todo, len(steps))
        self.assertFalse(os.path.exists(self.paths.data_root))
        self.assertEqual(self.mutating(), [])
        lib.execute(steps, True, lambda *_: None)
        self.assertTrue(os.path.isfile(self.paths.conf_file("perens_com")))
        self.assertTrue(os.path.isfile(self.paths.server_copy()))
        self.assertIn("hams_static", self.system.users)
        self.assertIn(["systemctl", "enable", "--now", "hams-static@perens_com.service"], self.system.commands)
        again = ctl.build_create_steps(self.spec, self.paths, self.system)
        self.assertEqual(lib.execute(again, True, lambda *_: None), 0)

    def test_the_installed_server_copy_is_byte_identical_to_the_source(self):
        lib.execute(ctl.build_create_steps(self.spec, self.paths, self.system), True, lambda *_: None)
        with open(self.paths.server_copy(), "rb") as copy, open(os.path.join(HERE, "static_site_server.py"), "rb") as source:
            self.assertEqual(copy.read(), source.read())

    def test_the_manifest_unit_is_sandboxed_loopback_only_and_has_no_write_path(self):
        unit = next(e for e in infra.MANIFEST["static_files"] if e["path"].endswith("hams-static@.service"))
        text = unit["content"]
        self.assertEqual(unit["host_class"], "odoo_tenants")
        self.assertNotIn("external_fetch", unit)
        for required in ("User=hams_static", "ProtectSystem=strict", "NoNewPrivileges=true", "CapabilityBoundingSet=\n",
                         "SocketBindDeny=any", "IPAddressDeny=any", "IPAddressAllow=localhost", "InaccessiblePaths=/opt/hams",
                         "ExecStart=/usr/bin/python3 /usr/local/lib/hams-static/static_site_server.py "
                         "--config /etc/hams-static/%i.json"):
            self.assertIn(required, text)
        self.assertNotIn("ReadWritePaths", text)

    def test_apply_needs_root_and_the_odoo_tenants_host_class(self):
        with mock.patch("os.geteuid", return_value=1000):
            with self.assertRaises(SystemExit):
                ctl.main(["create", SPEC, "--apply"], system=self.system, paths=self.paths)
        with mock.patch("os.geteuid", return_value=0), mock.patch.object(infra, "host_classes", return_value=set()):
            with self.assertRaises(SystemExit):
                ctl.main(["create", SPEC, "--apply"], system=self.system, paths=self.paths)


class InstallTests(Base):
    def test_filtered_files_leave_out_dotfiles_secrets_dumps_keys_logs_scripts_and_links(self):
        source = self.tree()
        copy, excluded = ctl.filtered_files(source)
        self.assertEqual(sorted(copy), ["static/a.pdf", "static/dir/b.html", "static/vid.webm"])
        left_out = {os.path.basename(p.rstrip("/")) for p, _ in excluded}
        for name in (".env", ".git", "wp-config.php", "dump.sql", "Notes.log", "id_rsa", "pw"):
            self.assertIn(name, left_out)

    def test_install_copies_the_filtered_tree_atomically_with_checksums_and_is_idempotent(self):
        source = self.tree()
        steps, copy, excluded = ctl.build_install_steps(self.spec, self.paths, self.system, source, chown=lambda p: None)
        self.assertEqual(lib.execute(steps, False, lambda *_: None), 1)
        self.assertFalse(os.path.exists(self.paths.site_dir("perens_com")))
        lib.execute(steps, True, lambda *_: None)
        target = os.path.join(self.paths.site_dir("perens_com"), "static")
        self.assertEqual(sorted(os.listdir(target)), ["a.pdf", "dir", "vid.webm"])
        self.assertFalse(os.path.exists(os.path.join(target, ".env")))
        self.assertEqual(oct(os.stat(os.path.join(target, "a.pdf")).st_mode & 0o777), "0o644")
        manifest = open(os.path.join(self.paths.site_dir("perens_com"), "MANIFEST.sha256"), encoding="utf-8").read()
        self.assertIn("static/dir/b.html", manifest)
        again, _, _ = ctl.build_install_steps(self.spec, self.paths, self.system, source, chown=lambda p: None)
        self.assertEqual(lib.execute(again, True, lambda *_: None), 0)
        with open(os.path.join(source, "static", "a.pdf"), "wb") as handle:
            handle.write(b"changed")
        changed, _, _ = ctl.build_install_steps(self.spec, self.paths, self.system, source, chown=lambda p: None)
        self.assertEqual(lib.execute(changed, False, lambda *_: None), 1)

    def test_the_installed_tree_is_what_the_server_serves_and_refuses(self):
        import http.client
        import threading

        source = self.tree()
        steps, _, _ = ctl.build_install_steps(self.spec, self.paths, self.system, source, chown=lambda p: None)
        lib.execute(steps, True, lambda *_: None)
        config = json.loads(ctl.render_config(self.spec, self.paths))
        config["port"] = 0
        server = srv.Server(("127.0.0.1", 0), config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]

        def status(path):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", path)
            code = conn.getresponse().status
            conn.close()
            return code

        self.assertEqual(status("/static/a.pdf"), 200)
        self.assertEqual(status("/static/dir/b.html"), 200)
        for refused in ("/static/.env", "/static/MANIFEST.sha256", "/MANIFEST.sha256", "/static/../MANIFEST.sha256"):
            self.assertEqual(status(refused), 404, refused)

    def test_an_empty_source_is_refused(self):
        os.makedirs(os.path.join(self.tmp, "empty", "static"))
        with self.assertRaises(lib.SpecError):
            ctl.build_install_steps(self.spec, self.paths, self.system, os.path.join(self.tmp, "empty"))

    def test_main_status_and_unknown_spec(self):
        lines = []
        self.system.active.add("hams-static@perens_com.service")
        with mock.patch.object(ctl, "probe", return_value=404):
            self.assertEqual(ctl.main(["status", SPEC], out=lines.append, system=self.system, paths=self.paths), 0)
        self.assertIn("active", lines[0])
        self.assertEqual(ctl.main(["status", os.path.join(self.tmp, "none.json")], out=lines.append, system=self.system,
                                  paths=self.paths), 2)


class OutputTests(Base):
    def test_main_plan_prints_steps_and_says_it_is_a_plan(self):
        lines = []
        self.assertEqual(ctl.main(["create", SPEC], out=lines.append, system=self.system, paths=self.paths), 0)
        text = "\n".join(lines)
        self.assertIn("plan only; add --apply", text)
        self.assertIn("os_user", text)
        self.assertEqual(self.mutating(), [])


if __name__ == "__main__":
    unittest.main()
