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
import datetime
import hashlib
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


class EndToEndTests(_Env):
    """Tests [@ANCHOR: b2_backup:init], [@ANCHOR: b2_backup:backup], [@ANCHOR: b2_backup:restore_test],
    [@ANCHOR: b2_backup:check] and [@ANCHOR: b2_backup:password_fingerprint]
    with real kopia and real PostgreSQL."""

    @classmethod
    def setUpClass(cls):
        for binary in ("kopia", "pg_dump", "pg_restore", "psql", "createdb", "dropdb"):
            _require(binary)
        cls.database = f"b2bk_test_{os.getpid()}"
        subprocess.run(["createdb", cls.database], check=True)
        subprocess.run(["psql", "-q", "-d", cls.database, "-c",
                        "create table t(i int); insert into t select generate_series(1,100)"], check=True)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["dropdb", "--if-exists", cls.database], check=False)

    def setUp(self):
        super().setUp()
        # An Odoo-style content-addressed filestore: each file is named by the SHA-1 of its content.
        self.filestore = self.t("filestore")
        for i in range(12):
            body = os.urandom(2000 + i)
            digest = hashlib.sha1(body).hexdigest()  # burn-ignore-legacy-protocol-hash: Odoo filestore naming
            os.makedirs(os.path.join(self.filestore, digest[:2]), exist_ok=True)
            with open(os.path.join(self.filestore, digest[:2], digest), "wb") as f:
                f.write(body)
        # Odoo's checklist/ bookkeeping: SHA-1-looking names whose content is NOT that hash.
        for i in range(12):
            name = hashlib.sha1(b"attachment-%d" % i).hexdigest()  # burn-ignore-legacy-protocol-hash: Odoo naming
            os.makedirs(os.path.join(self.filestore, "checklist", name[:2]), exist_ok=True)
            with open(os.path.join(self.filestore, "checklist", name[:2], name), "wb") as f:
                f.write(b"not the hash of this")
        self.etc = self.t("etc")
        os.makedirs(os.path.join(self.etc, "keys"))
        # A symlink such as letsencrypt's live/privkey.pem -> ../archive/... (here a dangling one, as in a copy).
        os.symlink("../archive/privkey1.pem", os.path.join(self.etc, "keys", "privkey.pem"))
        with open(os.path.join(self.etc, "keys", "daemon.key"), "w") as f:
            f.write("daemon-key\n")
        with open(os.path.join(self.etc, "keys", "b2_backup.env"), "w") as f:
            f.write("KOPIA_PASSWORD=must-not-be-backed-up\n")
        self.cfg_path = self.config(
            databases=[self.database],
            paths=[{"name": "filestore_hams_prod", "path": self.filestore, "content_addressed": True},
                   {"name": "etc", "path": self.etc, "exclude": ["keys/b2_backup.env"]}],
            restore_test={"sample_files": 5, "verify_percent": 100, "database": True})

    def init_and_backup(self):
        self.assertEqual(self.run_main(self.cfg_path, "init")[0], 0)
        code, out, err = self.run_main(self.cfg_path, "backup")
        self.assertEqual(code, 0, out + err)
        return out

    def kopia(self, *args):
        cfg = bb.load_config(self.cfg_path)
        env = bb.kopia_env(cfg, {"KOPIA_PASSWORD": PASSWORD})
        return subprocess.run(["kopia"] + list(args), env=env, capture_output=True, text=True, check=True).stdout

    def test_full_cycle_backup_restore_test_and_check(self):
        out = self.init_and_backup()
        self.assertIn(f"ok   db_{self.database}", out)
        status = _load(self.t("state", "backup_status.json"))
        self.assertTrue(status["ok"])
        self.assertEqual({s["source"] for s in status["sources"]},
                         {"filestore_hams_prod", "etc", f"db_{self.database}"})
        # The password and B2 key file is excluded; the other key is there.
        cfg = bb.load_config(self.cfg_path)
        env = bb.kopia_env(cfg, {"KOPIA_PASSWORD": PASSWORD})
        listing = self.kopia("snapshot", "list", self.etc, "--json")
        root = json.loads(listing)[-1]["rootEntry"]["obj"]
        files = bb._relative_files(cfg, env, root, bb.Runner())
        self.assertIn("keys/daemon.key", files)
        self.assertNotIn("keys/b2_backup.env", files)
        # Nothing plaintext is left behind in staging, and the repository holds none of the content.
        self.assertFalse(os.path.exists(os.path.join(self.t("state"), "staging", f"db_{self.database}")))
        # Retention is applied.
        policy = self.kopia("policy", "show", "--global", "--json")
        self.assertEqual(json.loads(policy)["retention"]["keepDaily"], 14)
        self.assertEqual(json.loads(policy)["retention"]["keepMonthly"], 12)
        # Restore test restores real files and the database, then cleans up.
        code, out, err = self.run_main(self.cfg_path, "restore-test")
        self.assertEqual(code, 0, out + err)
        report = _load(self.t("state", "restore_test_status.json"))
        self.assertTrue(report["ok"])
        self.assertEqual(report["database"]["database"], self.database)
        self.assertGreater(report["database"]["tables"], 0)
        self.assertTrue(all(s["verified"] > 0 for s in report["samples"]))
        self.assertFalse(os.path.exists(self.t("state", "restore-scratch")))
        scratch_dbs = subprocess.run(["psql", "-At", "-d", "postgres", "-c",
                                      f"select datname from pg_database where datname like 'b2restoretest_%{self.database}'"],
                                     capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(scratch_dbs, "")
        self.assertEqual(self.run_main(self.cfg_path, "check")[0], 0)

    def test_restore_test_samples_every_file_so_checklist_and_symlinks_cannot_fail_it(self):
        raw = _load(self.cfg_path)
        raw["restore_test"]["sample_files"] = 500
        with open(self.cfg_path, "w") as f:
            json.dump(raw, f)
        self.init_and_backup()
        code, out, err = self.run_main(self.cfg_path, "restore-test")
        self.assertEqual(code, 0, out + err)
        report = _load(self.t("state", "restore_test_status.json"))
        self.assertTrue(report["ok"], report.get("problems"))
        by = {s["source"]: s for s in report["samples"]}
        self.assertEqual(by["filestore_hams_prod"]["sampled"], 12)

    def test_second_run_is_incremental_and_same_day_reruns_replace_rather_than_pile_up(self):
        self.init_and_backup()
        with open(os.path.join(self.filestore, "late"), "w") as f:
            f.write("new file")
        self.assertEqual(self.run_main(self.cfg_path, "backup")[0], 0)
        snaps = json.loads(self.kopia("snapshot", "list", self.filestore, "--json"))
        # Retention keeps one snapshot per day, so a rerun the same day supersedes the earlier one.
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[-1]["stats"]["nonCachedFiles"], 1, "only the new file is read")
        self.assertEqual(snaps[-1]["stats"]["cachedFiles"], 24, "unchanged files must not be re-read")
        self.assertEqual(snaps[-1]["rootEntry"]["summ"]["files"], 25)

    def test_changed_password_file_stops_the_run(self):
        self.init_and_backup()
        self.write_env("a-different-password")
        code, _, err = self.run_main(self.cfg_path, "backup")
        self.assertEqual(code, 1)
        self.assertIn("differs from the one recorded at init", err)
        code, _, err = self.run_main(self.cfg_path, "restore-test")
        self.assertEqual(code, 1)
        self.assertIn("differs", err)

    def test_init_cannot_run_twice(self):
        self.assertEqual(self.run_main(self.cfg_path, "init")[0], 0)
        code, _, err = self.run_main(self.cfg_path, "init")
        self.assertEqual(code, 1)
        self.assertIn("already initialised", err)

    def test_backup_before_init_fails(self):
        code, _, err = self.run_main(self.cfg_path, "backup")
        self.assertEqual(code, 1)
        self.assertIn("run `b2_backup.py init` first", err)

    def test_a_missing_source_fails_the_run_but_the_others_are_still_backed_up(self):
        self.assertEqual(self.run_main(self.cfg_path, "init")[0], 0)
        shutil.rmtree(self.filestore)
        code, out, err = self.run_main(self.cfg_path, "backup")
        self.assertEqual(code, 1)
        self.assertIn("filestore_hams_prod", err)
        self.assertIn("ok   etc", out)
        status = _load(self.t("state", "backup_status.json"))
        self.assertFalse(status["ok"])

    def test_a_failed_dump_never_becomes_a_snapshot(self):
        self.assertEqual(self.run_main(self.cfg_path, "init")[0], 0)
        cfg_path = self.config(databases=["no_such_database_here"])
        code, out, _ = self.run_main(cfg_path, "backup")
        self.assertEqual(code, 1)
        self.assertIn("FAIL db_no_such_database_here", out)
        cfg = bb.load_config(cfg_path)
        env = bb.kopia_env(cfg, {"KOPIA_PASSWORD": PASSWORD})
        sources = [s for s in bb.build_sources(cfg) if s.kind == "db"]
        self.assertEqual(bb.list_snapshots(cfg, env, sources[0], bb.Runner()), [])

    def test_restore_test_catches_a_filestore_file_whose_content_does_not_match_its_name(self):
        self.assertEqual(self.run_main(self.cfg_path, "init")[0], 0)
        for name in [n for n in os.listdir(self.filestore) if n != "checklist"]:
            directory = os.path.join(self.filestore, name)
            for entry in os.listdir(directory):
                with open(os.path.join(directory, entry), "wb") as f:
                    f.write(b"silently corrupted")
        self.assertEqual(self.run_main(self.cfg_path, "backup")[0], 0)
        code, _, err = self.run_main(self.cfg_path, "restore-test")
        self.assertEqual(code, 1)
        self.assertIn("wrong content", err)
        self.assertFalse(_load(self.t("state", "restore_test_status.json"))["ok"])

    def test_check_flags_a_stale_or_failed_backup(self):
        self.init_and_backup()
        self.run_main(self.cfg_path, "restore-test")
        cfg = bb.load_config(self.cfg_path)
        self.assertEqual(bb.cmd_check(cfg, bb.Runner()), 0)
        later = bb.now_utc() + datetime.timedelta(hours=40)
        with self.assertRaises(bb.BackupError) as ctx:
            bb.cmd_check(cfg, bb.Runner(), now=later)
        self.assertIn("backup is 40 hours old", str(ctx.exception))
        status = _load(cfg.status_path)
        status["ok"] = False
        bb.write_json(cfg.status_path, status)
        with self.assertRaises(bb.BackupError) as ctx:
            bb.cmd_check(cfg, bb.Runner())
        self.assertIn("last backup failed", str(ctx.exception))

    def test_check_before_any_run_fails(self):
        code, _, err = self.run_main(self.cfg_path, "check")
        self.assertEqual(code, 1)
        self.assertIn("no backup has ever been recorded", err)


class TenantArchiveOffBoxTests(_Env):
    """Tests [@ANCHOR: b2_backup:tenant_archive_freshness] and [@ANCHOR: b2_backup:restore_tenant_archives]:
    the tenant archives reach the (encrypted) repository, come back byte for byte, and a missing upload is flagged."""

    @classmethod
    def setUpClass(cls):
        _require("kopia")

    def make_set(self, tenant, stamp, payload=b"dump-bytes"):
        directory = self.t("archives", tenant, stamp)
        os.makedirs(directory)
        files = {}
        for name, body in (("db.dump", payload), ("filestore.tar.gz", b"tar-" + payload)):
            with open(os.path.join(directory, name), "wb") as f:
                f.write(body)
            files[name] = {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
        with open(os.path.join(directory, "MANIFEST.json"), "w") as f:
            json.dump({"tenant": tenant, "created": stamp, "files": files}, f)
        return directory

    def setUp(self):
        super().setUp()
        self.make_set("perens_com", "20261004T074141Z")
        self.make_set("parking", "20261004T074140Z", b"other")
        self.cfg_path = self.config(paths=[{"name": "tenant_archives", "path": self.t("archives"), "optional": True}])
        self.assertEqual(self.run_main(self.cfg_path, "init")[0], 0)
        code, out, err = self.run_main(self.cfg_path, "backup")
        self.assertEqual(code, 0, out + err)
        self.cfg = bb.load_config(self.cfg_path)

    def entry(self):
        return [r for r in _load(self.cfg.status_path)["sources"] if r["source"] == "tenant_archives"][0]

    def test_backup_records_which_archive_set_it_uploaded(self):
        self.assertEqual(self.entry()["newest_archive_set"], "20261004T074141Z")
        self.assertTrue(self.entry()["ok"])

    def test_restore_downloads_the_copy_and_every_checksum_matches(self):
        dest = self.t("restored")
        code, out, err = self.run_main(self.cfg_path, "restore-tenant-archives", "--dest", dest)
        self.assertEqual(code, 0, out + err)
        self.assertIn("restored 2 archive sets (4 files, all checksums match), newest 20261004T074141Z", out)
        self.assertIn("--backup-root", out)
        with open(os.path.join(dest, "perens_com", "20261004T074141Z", "db.dump"), "rb") as f:
            self.assertEqual(f.read(), b"dump-bytes")
        # A destination that exists is refused, so a restore can never overwrite a live directory.
        code, _, err = self.run_main(self.cfg_path, "restore-tenant-archives", "--dest", dest)
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

    def test_a_damaged_copy_is_detected(self):
        sets, files, problems = bb.verify_archive_sets(self.t("archives"))
        self.assertEqual((sets, files, problems), (2, 4, []))
        with open(self.t("archives", "perens_com", "20261004T074141Z", "db.dump"), "ab") as f:
            f.write(b"x")
        os.remove(self.t("archives", "parking", "20261004T074140Z", "filestore.tar.gz"))
        _, _, problems = bb.verify_archive_sets(self.t("archives"))
        self.assertTrue(any("db.dump: checksum mismatch" in p for p in problems))
        self.assertTrue(any("filestore.tar.gz: missing" in p for p in problems))

    def test_weekly_restore_test_downloads_and_verifies_the_tenant_archives(self):
        code, out, err = self.run_main(self.cfg_path, "restore-test")
        self.assertEqual(code, 0, out + err)
        report = _load(self.cfg.restore_status_path)
        self.assertEqual(report["tenant_archives"]["sets"], 2)
        self.assertFalse(os.path.exists(self.cfg.scratch_dir))

    def test_check_flags_an_archive_set_that_never_reached_the_repository(self):
        self.run_main(self.cfg_path, "restore-test")
        now = bb.now_utc()
        fresh = (now - datetime.timedelta(hours=2)).strftime("%Y%m%dT%H%M%SZ")
        self.make_set("perens_com", fresh)
        # Made two hours ago and not uploaded yet: the next nightly run will take it, no alert.
        self.assertEqual(bb.cmd_check(self.cfg, bb.Runner(), now=now), 0)
        # Still not uploaded 40 hours later while the rest of the backup stays green: that is the alert.
        old = (now - datetime.timedelta(hours=40)).strftime("%Y%m%dT%H%M%SZ")
        shutil.rmtree(self.t("archives", "perens_com", fresh))
        self.make_set("perens_com", old)
        with self.assertRaises(bb.BackupError) as ctx:
            bb.cmd_check(self.cfg, bb.Runner(), now=now)
        self.assertIn(f"tenant archive set {old} is 40 hours old and not off-box", str(ctx.exception))
        # After a real upload (a partial run is enough) the check is green again.
        code, out, err = self.run_main(self.cfg_path, "backup", "--only", "tenant_archives")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.entry()["newest_archive_set"], old)
        self.assertEqual(bb.cmd_check(self.cfg, bb.Runner(), now=now), 0)

    def test_check_flags_a_skipped_or_failed_tenant_archive_source(self):
        status = _load(self.cfg.status_path)
        status["sources"] = [r for r in status["sources"] if r["source"] != "tenant_archives"]
        bb.write_json(self.cfg.status_path, status)
        self.run_main(self.cfg_path, "restore-test")
        old = (bb.now_utc() - datetime.timedelta(hours=50)).strftime("%Y%m%dT%H%M%SZ")
        self.make_set("parking", old)
        with self.assertRaises(bb.BackupError) as ctx:
            bb.cmd_check(self.cfg, bb.Runner())
        self.assertIn("holds no good tenant_archives snapshot", str(ctx.exception))

    def test_no_archives_directory_is_not_an_alert(self):
        shutil.rmtree(self.t("archives"))
        self.assertEqual(bb.tenant_archive_problems(self.cfg, bb.Runner(), bb.now_utc()), [])

    def test_partial_backup_never_advances_the_nightly_freshness(self):
        before = _load(self.cfg.status_path)["finished"]
        self.run_main(self.cfg_path, "backup", "--only", "tenant_archives")
        self.assertEqual(_load(self.cfg.status_path)["finished"], before)
        code, _, err = self.run_main(self.cfg_path, "backup", "--only", "nope")
        self.assertEqual(code, 1)
        self.assertIn("names no configured source", err)


class QsoRecordingsBackupTests(_Env):
    """Tests [@ANCHOR: b2_backup:restore_recordings]: the Simulated Band recordings reach the repository without their
    in-progress *.tmp files, are kept forever, and a sample of the newest ones comes back with identical sha256."""

    @classmethod
    def setUpClass(cls):
        _require("kopia")

    NAMES = ["aaaa", "bbbb", "cccc"]

    def setUp(self):
        super().setUp()
        self.rec = self.t("qso_recordings")
        os.makedirs(self.rec)
        now = datetime.datetime.now().timestamp()
        for index, name in enumerate(self.NAMES):
            for ext, body in ((".opus", os.urandom(3000 + index)), (".json", b'{"n": %d}' % index)):
                path = os.path.join(self.rec, name + ext)
                with open(path, "wb") as f:
                    f.write(body)
                os.utime(path, (now - 3600 * (10 - index),) * 2)  # all finalised well before the snapshot
        with open(os.path.join(self.rec, "dddd.opus.tmp"), "wb") as f:  # in progress: must never be snapshotted
            f.write(b"partial")
        self.cfg_path = self.config(
            paths=[{"name": "qso_recordings", "path": self.rec, "optional": True, "exclude": ["*.tmp"], "keep_forever": True}],
            restore_test={"sample_files": 5, "verify_percent": 100, "database": False, "recordings_files": 4})
        self.cfg = bb.load_config(self.cfg_path)
        self.assertEqual(self.run_main(self.cfg_path, "init")[0], 0)
        code, out, err = self.run_main(self.cfg_path, "backup")
        self.assertEqual(code, 0, out + err)

    def kopia(self, *args):
        env = bb.kopia_env(self.cfg, {"KOPIA_PASSWORD": PASSWORD})
        return subprocess.run(["kopia"] + list(args), env=env, capture_output=True, text=True, check=True).stdout

    def test_in_progress_files_are_not_snapshotted(self):
        listing = self.kopia("ls", "-r", bb.latest_snapshot(self.cfg, bb.kopia_env(self.cfg, {"KOPIA_PASSWORD": PASSWORD}),
                                                            bb.build_sources(self.cfg)[0], bb.Runner())["rootEntry"]["obj"])
        self.assertIn("aaaa.opus", listing)
        self.assertIn("aaaa.json", listing)
        self.assertNotIn(".tmp", listing)

    def test_policy_keeps_every_snapshot_forever(self):
        argv = bb.keep_forever_policy_argv(self.cfg, self.rec)
        for flag in ("--keep-latest", "--keep-hourly", "--keep-daily", "--keep-weekly", "--keep-monthly", "--keep-annual"):
            self.assertIn(f"{flag}={bb.KEEP_EVERYTHING}", argv)
        self.assertFalse([a for a in argv if "delete" in a or "expire" in a or "prune" in a])
        shown = self.kopia("policy", "show", self.rec)
        self.assertIn(str(bb.KEEP_EVERYTHING), shown)
        self.assertNotIn(" 14 ", shown.split("Retention:")[1].split("Scheduling")[0].replace("(defined for this target)", ""))

    def test_status_records_the_snapshot_time_for_the_readiness_check(self):
        entry = [r for r in _load(self.cfg.status_path)["sources"] if r["source"] == "qso_recordings"][0]
        self.assertTrue(entry["ok"])
        self.assertTrue(entry["snapshot_time"])

    def test_restore_test_compares_sha256_of_the_newest_recordings(self):
        code, out, err = self.run_main(self.cfg_path, "restore-test")
        self.assertEqual(code, 0, out + err)
        report = _load(self.cfg.restore_status_path)["recordings"]
        self.assertEqual((report["sampled"], report["verified"], report["missing_live"], report["changed_since"]), (4, 4, 0, 0))
        self.assertFalse(os.path.exists(self.cfg.scratch_dir))
        for name in self.NAMES:  # no recording name reaches the output
            self.assertNotIn(name, out + err)

    def test_a_restored_recording_that_differs_from_the_live_file_fails(self):
        live = os.path.join(self.rec, "cccc.opus")  # the newest by mtime is aaaa? make cccc newest and altered
        past = os.path.getmtime(live)
        with open(live, "r+b") as f:
            f.write(b"X")
        os.utime(live, (past, past))
        os.utime(live, (past, past))
        newest = datetime.datetime.now().timestamp() - 600
        os.utime(live, (newest, newest))
        code, out, err = self.run_main(self.cfg_path, "restore-test")
        self.assertEqual(code, 1)
        self.assertIn("sha256 mismatch", err)

    def test_a_deleted_live_recording_is_counted_not_failed(self):
        os.remove(os.path.join(self.rec, "cccc.opus"))
        code, out, err = self.run_main(self.cfg_path, "restore-test")
        self.assertEqual(code, 0, out + err)
        report = _load(self.cfg.restore_status_path)["recordings"]
        self.assertEqual(report["missing_live"], 1)
        self.assertEqual(report["verified"], 3)

    def test_an_empty_directory_is_a_clean_snapshot_and_restore_test(self):
        for name in os.listdir(self.rec):
            os.remove(os.path.join(self.rec, name))
        code, out, err = self.run_main(self.cfg_path, "backup")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.run_main(self.cfg_path, "restore-test")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(_load(self.cfg.restore_status_path)["recordings"]["sampled"], 0)

    def test_absent_directory_is_skipped_like_any_optional_source(self):
        shutil.rmtree(self.rec)
        self.assertEqual(bb.build_sources(self.cfg), [])


if __name__ == "__main__":
    unittest.main()
