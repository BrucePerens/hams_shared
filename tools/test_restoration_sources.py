# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests of the restoration system's source adapters, with fake backups (directories and recorded command results)."""
import json
import os
import subprocess

import pytest

from restoration_system import secrets_input as si
from restoration_system import sources as src
from restoration_system import sourceset as ss
from restoration_system import profile as prof
from restoration_system.runner import RestorationError, Runner


class FakeRunner(Runner):
    """Returns canned output for a command; records the argument lists and the secret variable NAMES it was given."""

    def __init__(self, answers):
        super().__init__(log=lambda line: None)
        self.answers, self.calls = answers, []

    def run(self, argv, secret_env=None, **kw):
        self.calls.append((list(argv), sorted(secret_env or {})))
        for key, out in self.answers.items():
            if key in " ".join(argv):
                return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def test_secrets_never_show_values():
    secrets = si.Secrets({si.KOPIA: "correct-horse"})
    assert "correct-horse" not in repr(secrets)
    assert secrets.kopia_env()["KOPIA_PASSWORD"] == "correct-horse"


def test_ask_fails_without_a_terminal(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("no tty")
    monkeypatch.setattr(os, "open", refuse)
    with pytest.raises(RestorationError, match="no terminal"):
        si.ask("x")


def test_secret_goes_only_in_environment_never_argv():
    runner = FakeRunner({"snapshot list": "[]"})
    secrets = si.Secrets({si.KOPIA: "pw-123"})
    repo = src.KopiaRepo(runner, secrets, "/tmp/none", ["filesystem", "--path=/x"], "t")
    repo.snapshots()
    argv, names = runner.calls[0]
    assert "pw-123" not in " ".join(argv)
    assert "KOPIA_PASSWORD" in names


def test_kopia_marker_refuses_missing_and_foreign_directory(tmp_path):
    with pytest.raises(RestorationError, match="does not exist"):
        src.SourceCheck.kopia_marker(str(tmp_path / "nope"))
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "file").write_text("x")
    with pytest.raises(RestorationError, match="not a kopia repository"):
        src.SourceCheck.kopia_marker(str(tmp_path / "d"))


def test_kopia_unencrypted_and_empty_are_refused():
    runner = FakeRunner({"repository status": json.dumps({"contentFormat": {"encryption": "NONE"}})})
    repo = src.KopiaRepo(runner, si.Secrets({si.KOPIA: "x"}), "/tmp/none", [], "t")
    with pytest.raises(RestorationError, match="not encrypted"):
        src.SourceCheck.kopia_encrypted(repo)
    with pytest.raises(RestorationError, match="no snapshots"):
        src.SourceCheck.kopia_not_empty([], "t")


def test_pgbackrest_marker_requires_encryption_header(tmp_path):
    info = tmp_path / "backup" / "s"
    info.mkdir(parents=True)
    with pytest.raises(RestorationError, match="missing or empty"):
        src.SourceCheck.pgbackrest_marker(str(tmp_path), "s")
    (info / "backup.info").write_text("[backrest]\nbackrest-format=5\n")
    with pytest.raises(RestorationError, match="not encrypted"):
        src.SourceCheck.pgbackrest_marker(str(tmp_path), "s")
    (info / "backup.info").write_bytes(b"Salted__12345678data")
    src.SourceCheck.pgbackrest_marker(str(tmp_path), "s")


def test_check_info_refuses_plaintext_and_empty():
    with pytest.raises(RestorationError, match="not encrypted"):
        src.PgBackRestRepo.check_info({"cipher": "none", "backup": [{}]})
    with pytest.raises(RestorationError, match="no readable backup"):
        src.PgBackRestRepo.check_info({"cipher": "aes-256-cbc", "backup": []})
    assert src.PgBackRestRepo.check_info({"cipher": "aes-256-cbc", "backup": [{"label": "a"}, {"label": "b"}], "status": {"code": 0}})["label"] == "b"


def test_restore_argv_has_pitr_settings_and_local_wal():
    repo = src.PgBackRestRepo(FakeRunner({}), si.Secrets(), "/tmp/s", "st", "aes-256-cbc", {"type": "posix", "path": "/r"}, "/pg")
    argv = repo.restore_argv("/d", "2026-11-09 03:00:00+00", "/var/lib/postgresql/wal_restore", "LABEL")
    joined = " ".join(argv)
    assert "--type=time" in argv and "--target=2026-11-09 03:00:00+00" in argv and "--target-action=promote" in argv
    assert "restore_command=cp /var/lib/postgresql/wal_restore/%f %p" in joined
    assert "--archive-mode=off" in argv and "--set=LABEL" in argv
    assert "archive-get" not in joined and "--type" not in " ".join(repo.restore_argv("/d", None, "/w"))


def test_wal_segment_arithmetic():
    assert src.next_segment("0000000100000000000000FF") == "000000010000000100000000"
    assert src.segment_name(2, src.segment_number("000000010000000100000002")) == "000000020000000100000002"


def test_newest_snapshot_selection():
    snaps = [{"startTime": "2026-01-01T00:00:00Z", "source": {"path": "/a"}, "id": "old", "tags": {"tag:kind": "files", "tag:name": "x"}},
             {"startTime": "2026-02-01T00:00:00Z", "source": {"path": "/a"}, "id": "new", "tags": {"tag:kind": "files", "tag:name": "x"}}]
    assert src.newest_by_path(snaps)["/a"]["id"] == "new"
    assert src.newest_by_tag(snaps, "files")["x"]["id"] == "new"


def test_open_local_refuses_missing_empty_and_plaintext_staging(tmp_path):
    profile = prof.load()
    runner, secrets = FakeRunner({}), si.Secrets()
    with pytest.raises(RestorationError, match="missing or empty"):
        ss.open_local(str(tmp_path / "absent"), runner, secrets, profile, str(tmp_path))
    (tmp_path / "empty").mkdir()
    with pytest.raises(RestorationError, match="missing or empty"):
        ss.open_local(str(tmp_path / "empty"), runner, secrets, profile, str(tmp_path))
    staging = tmp_path / "staging"
    (staging / "hams1-backup" / "db").mkdir(parents=True)
    with pytest.raises(RestorationError, match="no encrypted source"):
        ss.open_local(str(staging), runner, secrets, profile, str(tmp_path))


def test_apply_rsync_stat_sets_owner_and_mode(tmp_path):
    tree = tmp_path / "t"
    tree.mkdir()
    (tree / "a").write_text("x")
    uid, gid = os.getuid(), os.getgid()
    (tree / ".hams-rsync-stat.json").write_text(json.dumps({"a": "100640 0,0 %d:%d" % (uid, gid), "gone": "100644 0,0 1:1"}))
    assert ss.apply_rsync_stat(str(tree)) == 1
    assert (os.stat(tree / "a").st_mode & 0o7777) == 0o640
    assert not (tree / ".hams-rsync-stat.json").exists()


def test_profile_requires_sections(tmp_path):
    bad = tmp_path / "p.json"
    bad.write_text('{"name": "x"}')
    with pytest.raises(RestorationError, match="lacks"):
        prof.load(str(bad))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 1e20, -1e20])
def test_parse_timestamp_refuses_an_epoch_that_is_not_a_date_with_a_value_error(bad):
    with pytest.raises(ValueError):
        src.parse_timestamp(bad)


def test_parse_timestamp_reads_an_epoch_and_an_rfc3339_time():
    assert src.parse_timestamp(0).year == 1970
    assert src.parse_timestamp("2026-11-09T12:00:00Z").hour == 12

# Class anchors (ADR 0113) exercised by the tests above.
# Tests [@ANCHOR: runner:runner_restoration_error_class]
# Tests [@ANCHOR: secrets_input:secrets_input_secrets_class]
# Tests [@ANCHOR: sources:sources_pg_back_rest_repo_class]
# Tests [@ANCHOR: sources:sources_source_check_class]
# Tests [@ANCHOR: sources:sources_kopia_repo_class]
# Tests [@ANCHOR: sourceset:sourceset_backup_set_class]
# Tests [@ANCHOR: sourceset:sourceset_db_plan_class]
# Tests [@ANCHOR: sourceset:sourceset_file_item_class]
# Tests [@ANCHOR: runner:runner_runner_class]


def _fetch_wal_repo(present):
    repo = src.PgBackRestRepo(FakeRunner({}), si.Secrets({}), "/nonexistent", "s", "aes-256-cbc", {"type": "posix", "path": "/r"}, "/p")

    def call(*args, check=True, capture=True):
        name = args[1]
        return subprocess.CompletedProcess(args, 0 if name in present else 1, stdout="", stderr="")
    repo.call = call
    return repo


def test_fetch_wal_refuses_a_gap_in_the_middle(tmp_path):
    names = ["000000010000000000000010", "000000010000000000000011", "000000010000000000000013"]
    repo = _fetch_wal_repo(set(names))
    with pytest.raises(RestorationError, match="missing"):
        repo.fetch_wal({"archive": {"start": names[0]}}, {"archive": [{"max": names[-1]}]}, str(tmp_path))


def test_fetch_wal_accepts_a_contiguous_range(tmp_path):
    names = ["000000010000000000000010", "000000010000000000000011", "000000010000000000000012"]
    repo = _fetch_wal_repo(set(names))
    assert repo.fetch_wal({"archive": {"start": names[0]}}, {"archive": [{"max": names[-1]}]}, str(tmp_path)) == names


def test_fetch_wal_accepts_a_timeline_switch(tmp_path):
    present = {"000000010000000000000010", "000000010000000000000011", "000000020000000000000011", "000000020000000000000012", "00000002.history"}
    repo = _fetch_wal_repo(present)
    got = repo.fetch_wal({"archive": {"start": "000000010000000000000010"}}, {"archive": [{"max": "000000020000000000000012"}]}, str(tmp_path))
    assert "000000020000000000000012" in got


def _snap(path, start="2026-01-01T00:00:00Z"):
    return {"id": "i" + path, "startTime": start, "source": {"path": path}}


def test_file_items_refuses_a_listed_path_with_no_snapshot():
    profile = {"b2_kopia_paths": ["/a", "/b"], "b2_kopia_files": {}}
    bs = ss.BackupSet("b2", FakeRunner({}), si.Secrets({}), profile, "/w")
    bs.kopia_snapshots = [_snap("/a")]
    with pytest.raises(RestorationError, match="/b"):
        bs.file_items()
    bs.kopia_snapshots = [_snap("/a"), _snap("/b")]
    assert [i.path for i in bs.file_items()] == ["/a", "/b"]


def test_file_items_refuses_a_listed_file_and_local_source_with_no_snapshot():
    bs = ss.BackupSet("b2", FakeRunner({}), si.Secrets({}), {"b2_kopia_paths": [], "b2_kopia_files": {"secret": "/s"}}, "/w")
    with pytest.raises(RestorationError, match="secret"):
        bs.file_items()
    local = ss.BackupSet("local", FakeRunner({}), si.Secrets({}), {"local_files_sources": {"filestore": "/f"}}, "/w")
    with pytest.raises(RestorationError, match="filestore"):
        local.file_items()


def test_apply_rsync_stat_refuses_a_path_outside_the_tree(tmp_path):
    tree = tmp_path / "t"
    tree.mkdir()
    outside = tmp_path / "victim"
    outside.write_text("x")
    link = tree / "lnk"
    link.symlink_to(tmp_path)
    for rel in (str(outside), "../victim", "lnk/victim"):
        (tree / ".hams-rsync-stat.json").write_text(json.dumps({rel: "100600 0,0 0:0"}))
        with pytest.raises(RestorationError, match="outside"):
            ss.apply_rsync_stat(str(tree))
