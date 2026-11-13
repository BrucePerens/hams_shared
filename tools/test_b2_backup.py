#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for b2_backup.py.

The end-to-end tests run the real tool against the real kopia binary and a real PostgreSQL
database, the same code path production runs. The one difference is the storage location: kopia's
`filesystem` backend (a directory) stands in for the B2 bucket, because no Backblaze credentials
exist on a test host. The S3 argument assembly is tested as a pure function and the S3 path itself
was NOT exercised against a live bucket (see docs/runbooks/b2_backup.md in hams_com).
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import b2_backup as bb

PASSWORD = "unit-test-repository-password"


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _load(path):
    return json.loads(_read(path))


def _require(binary):
    if not shutil.which(binary):
        raise AssertionError(f"{binary} is required to run these tests (provisioned by infrastructure.py)")


class _Env(unittest.TestCase):
    """A scratch state directory, repository, source tree, tenant spec and env file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="b2bk-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.t = lambda *p: os.path.join(self.tmp, *p)
        os.makedirs(self.t("repo"))
        self.write_env(PASSWORD)

    def write_env(self, password):
        fd = os.open(self.t("b2_backup.env"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"# secrets\nKOPIA_PASSWORD={password}\n")

    def config(self, **overrides):
        raw = {
            "backend": {"type": "filesystem", "path": self.t("repo")},
            "env_file": self.t("b2_backup.env"),
            "state_dir": self.t("state"),
            "tenant_spec_dir": self.t("tenants.d"),
            "tenant_data_root": self.t("tenants"),
            "databases": [],
            "paths": [],
            "restore_test": {"sample_files": 5, "verify_percent": 100, "database": False},
        }
        raw.update(overrides)
        path = self.t("config.json")
        with open(path, "w") as f:
            json.dump(raw, f)
        return path

    def run_main(self, config, *args, plan=False):
        out, err = io.StringIO(), io.StringIO()
        argv = ["--config", config] + (["--plan"] if plan else []) + list(args)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bb.main(argv)
        return code, out.getvalue(), err.getvalue()


class ConfigAndArgumentTests(_Env):
    """Tests [@ANCHOR: b2_backup:tool]"""

    def test_s3_arguments_carry_endpoint_and_rate_limits_but_no_secret(self):
        path = self.config(backend={"type": "s3", "bucket": "hams-com-prod-backups",
                                    "endpoint": "s3.us-east-005.backblazeb2.com",
                                    "region": "us-east-1", "prefix": "files/"})
        cfg = bb.load_config(path)
        argv = bb.repo_connect_argv(cfg)
        self.assertIn("--endpoint=s3.us-east-005.backblazeb2.com", argv)
        self.assertIn("--bucket=hams-com-prod-backups", argv)
        self.assertIn("--prefix=files/", argv)
        self.assertIn("--max-upload-speed=5000000", argv)
        self.assertIn("--no-persist-credentials", argv)
        for token in bb.repo_create_argv(cfg) + argv:
            self.assertNotIn("AWS", token)
            self.assertNotIn(PASSWORD, token)
        self.assertEqual(set(bb.required_secret_names(cfg)),
                         {"KOPIA_PASSWORD", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"})

    def test_object_lock_arguments_only_when_configured(self):
        cfg = bb.load_config(self.config())
        self.assertFalse([a for a in bb.repo_create_argv(cfg) if a.startswith("--retention")])
        locked = bb.load_config(self.config(backend={
            "type": "s3", "bucket": "b", "endpoint": "s3.us-east-005.backblazeb2.com", "prefix": "files/",
            "object_lock": {"mode": "GOVERNANCE", "period": "720h"}}))
        self.assertIn("--retention-mode=GOVERNANCE", bb.repo_create_argv(locked))
        self.assertIn("--retention-period=720h", bb.repo_create_argv(locked))

    def test_retention_defaults_are_14_8_12(self):
        argv = bb.global_policy_argv(bb.load_config(self.config()))
        for flag in ("--keep-daily=14", "--keep-weekly=8", "--keep-monthly=12", "--compression=zstd"):
            self.assertIn(flag, argv)

    def test_s3_prefix_is_required_and_cannot_collide_with_pgbackrest(self):
        ep = "s3.us-east-005.backblazeb2.com"
        for prefix in (None, "", "files", "/files/", "hams_prod/", "hams_prod_enc/"):
            backend = {"type": "s3", "bucket": "hams-com-prod-backups", "endpoint": ep}
            if prefix is not None:
                backend["prefix"] = prefix
            with self.assertRaises(bb.ConfigError, msg=repr(prefix)):
                bb.load_config(self.config(backend=backend))

    def test_bad_configuration_is_refused(self):
        for backend in ({"type": "s3", "bucket": "b", "endpoint": "s3.amazonaws.com"},
                        {"type": "ftp"}, {"type": "s3", "endpoint": "s3.us-east-005.backblazeb2.com"}):
            with self.assertRaises(bb.ConfigError):
                bb.load_config(self.config(backend=backend))
        with self.assertRaises(bb.ConfigError):
            bb.load_config(self.config(databases=["x; drop database y"]))
        with self.assertRaises(bb.ConfigError):
            bb.load_config(self.config(paths=[{"name": "a", "path": "relative"}]))

    def test_tenants_are_discovered_from_specs_and_a_broken_spec_is_an_error(self):
        os.makedirs(self.t("tenants.d"))
        for name in ("perens_com", "parking"):
            with open(self.t("tenants.d", name + ".json"), "w") as f:
                json.dump({"name": name}, f)
        cfg = bb.load_config(self.config(databases=["hams_prod"]))
        names = [(s.name, s.kind) for s in bb.build_sources(cfg)]
        self.assertEqual(names, [("tenant_parking_filestore", "path"), ("db_parking", "db"),
                                 ("tenant_perens_com_filestore", "path"), ("db_perens_com", "db"),
                                 ("db_hams_prod", "db")])
        with open(self.t("tenants.d", "bad.json"), "w") as f:
            json.dump({"name": "Bad Name"}, f)
        with self.assertRaises(bb.ConfigError):
            bb.build_sources(cfg)

    def test_optional_path_sources_are_skipped_when_absent_and_required_ones_are_not(self):
        os.makedirs(self.t("archives"))
        paths = [{"name": "archives", "path": self.t("archives"), "optional": True},
                 {"name": "static", "path": self.t("no-such-static"), "optional": True},
                 {"name": "etc", "path": self.t("no-such-etc")}]
        cfg = bb.load_config(self.config(paths=paths))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            names = [s.name for s in bb.build_sources(cfg)]
        self.assertEqual(names, ["archives", "etc"], "an absent optional source is skipped, a required one is kept")
        self.assertIn("optional source static skipped", err.getvalue())
        self.assertNotIn("optional source archives", err.getvalue())
        # The kept, required, absent source still fails its own snapshot instead of vanishing from the night's run.
        required = [s for s in bb.build_sources(cfg) if s.name == "etc"][0]
        with self.assertRaises(bb.BackupError):
            bb.snapshot_source(cfg, {}, required, bb.Runner())

    def test_no_tenant_directory_means_no_tenants(self):
        self.assertEqual(bb.discover_tenants(bb.load_config(self.config())), [])

    def test_gen_password_never_overwrites_and_never_prints_the_value(self):
        target = self.t("pw")
        code, out, _ = self.run_main(self.config(), "gen-password", "--out", target)
        self.assertEqual(code, 0)
        value = _read(target).strip()
        self.assertGreaterEqual(len(value), 48)
        self.assertNotIn(value, out)
        self.assertEqual(oct(os.stat(target).st_mode & 0o777), "0o600")
        code, _, err = self.run_main(self.config(), "gen-password", "--out", target)
        self.assertEqual(code, 1)
        self.assertEqual(_read(target).strip(), value)
        self.assertIn("never replaces", err)


class PlanModeTests(_Env):
    """Tests [@ANCHOR: b2_backup:backup]"""

    def test_plan_prints_commands_and_changes_nothing(self):
        os.makedirs(self.t("fs"))
        path = self.config(paths=[{"name": "filestore_hams_prod", "path": self.t("fs")}],
                           databases=["hams_prod"],
                           backend={"type": "s3", "bucket": "b", "endpoint": "s3.us-east-005.backblazeb2.com", "prefix": "files/"})
        os.remove(self.t("b2_backup.env"))
        for command in ("init", "backup", "restore-test", "check"):
            code, out, err = self.run_main(path, command, plan=True)
            self.assertEqual(code, 0, err)
            self.assertIn("PLAN", out)
            self.assertFalse(os.path.exists(self.t("state")), f"{command} created the state directory")
        _, out, _ = self.run_main(path, "backup", plan=True)
        self.assertIn("kopia snapshot create --parallel=1", out)
        self.assertIn("pg_dump -Fc -Z 0 hams_prod", out)
        self.assertNotIn(PASSWORD, out)


class SingleFileBackupTests(_Env):
    """Tests [@ANCHOR: b2_backup:restore_file]: a single-file source (hams1's hams_crypto.secret) is uploaded on stdin,
    comes back with the same sha256 in the restore test, and is skipped when absent."""

    @classmethod
    def setUpClass(cls):
        _require("kopia")

    def setUp(self):
        super().setUp()
        self.secret = self.t("hams_crypto.secret")
        fd = os.open(self.secret, os.O_WRONLY | os.O_CREAT, 0o400)
        with os.fdopen(fd, "w") as f:
            f.write("unit-test-key-material-not-a-real-secret==")
        self.cfg_path = self.config(
            files=[{"name": "hams_crypto_secret", "path": self.secret, "optional": True}])
        self.cfg = bb.load_config(self.cfg_path)
        self.env = bb.kopia_env(self.cfg, {"KOPIA_PASSWORD": PASSWORD})
        self.assertEqual(self.run_main(self.cfg_path, "init")[0], 0)

    def test_the_file_is_snapshotted_and_no_copy_is_staged_on_disk(self):
        code, out, err = self.run_main(self.cfg_path, "backup")
        self.assertEqual(code, 0, out + err)
        snap = bb.latest_snapshot(self.cfg, self.env, bb.build_sources(self.cfg)[0], bb.Runner(plan=False))
        listing = subprocess.run(["kopia", "ls", "-r", snap["rootEntry"]["obj"]], env=self.env,
                                 capture_output=True, text=True, check=True).stdout
        self.assertIn("hams_crypto.secret", listing)
        staged = []
        for root, _dirs, names in os.walk(self.cfg.state_dir):
            staged += [n for n in names if "crypto" in n]
        self.assertEqual(staged, [])
        status = _load(self.cfg.status_path)
        self.assertTrue(status["ok"])
        self.assertNotIn("unit-test-key-material", json.dumps(status) + out + err)

    def test_restore_test_compares_the_sha256_with_the_live_file(self):
        self.assertEqual(self.run_main(self.cfg_path, "backup")[0], 0)
        code, out, err = self.run_main(self.cfg_path, "restore-test")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(_load(self.cfg.restore_status_path)["files"],
                         [{"file": "hams_crypto_secret", "compared": True}])
        self.assertNotIn("unit-test-key-material", out + err)

    def test_a_live_file_that_changed_after_the_snapshot_fails_the_restore_test(self):
        self.assertEqual(self.run_main(self.cfg_path, "backup")[0], 0)
        os.chmod(self.secret, 0o600)
        with open(self.secret, "w") as f:
            f.write("a-different-key")
        code, out, err = self.run_main(self.cfg_path, "restore-test")
        self.assertNotEqual(code, 0, out + err)
        self.assertFalse(_load(self.cfg.restore_status_path)["ok"])

    def test_absent_optional_file_is_skipped_but_a_required_one_fails(self):
        os.chmod(self.secret, 0o600)
        os.remove(self.secret)
        self.assertEqual(bb.build_sources(self.cfg), [])
        required = self.config(files=[{"name": "hams_crypto_secret", "path": self.secret}])
        code, out, err = self.run_main(required, "backup")
        self.assertNotEqual(code, 0, out + err)

    def test_bad_file_entries_are_refused(self):
        for bad in ({"name": "Bad Name", "path": self.secret}, {"name": "ok", "path": "relative"}):
            with self.assertRaises(bb.ConfigError):
                bb.load_config(self.config(files=[bad]))
        with self.assertRaises(bb.ConfigError):
            bb.load_config(self.config(paths=[{"name": "dup", "path": self.tmp}],
                                       files=[{"name": "dup", "path": self.secret}]))


if __name__ == "__main__":
    unittest.main()

# Class anchors (ADR 0113) exercised by the tests above.
# Tests [@ANCHOR: b2_backup:b2_backup__lock_class]
# Tests [@ANCHOR: b2_backup:b2_backup_runner_class]
# Tests [@ANCHOR: b2_backup:b2_backup_source_class]
# Tests [@ANCHOR: b2_backup:b2_backup_config_class]
# Tests [@ANCHOR: b2_backup:b2_backup_backup_error_class]
# Tests [@ANCHOR: b2_backup:b2_backup_config_error_class]
