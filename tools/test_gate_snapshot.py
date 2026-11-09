#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for gate_snapshot.py (ADR 0118) on real throwaway git repositories: a hams_com, a hams_open with a hams_shared
gitlink, each with an `origin`. The two build commands are replaced by a function that writes the outputs.

Tests [@ANCHOR: gate_snapshot:refuse_production]
Tests [@ANCHOR: gate_snapshot:origin_main]
Tests [@ANCHOR: gate_snapshot:recorded_shared_commit]
Tests [@ANCHOR: gate_snapshot:gate_dir_name]
Tests [@ANCHOR: gate_snapshot:create_trees]
Tests [@ANCHOR: gate_snapshot:artifact_output]
Tests [@ANCHOR: gate_snapshot:source_stamp]
Tests [@ANCHOR: gate_snapshot:artifact_problems]
Tests [@ANCHOR: gate_snapshot:build_artifact]
Tests [@ANCHOR: gate_snapshot:discover_modules]
Tests [@ANCHOR: gate_snapshot:make_shards]
Tests [@ANCHOR: gate_snapshot:shard_command]
Tests [@ANCHOR: gate_snapshot:verify_snapshot]
Tests [@ANCHOR: gate_snapshot:create_snapshot]
Tests [@ANCHOR: gate_snapshot:prune_snapshots]
Tests [@ANCHOR: gate_snapshot:main]
"""

import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

import gate_snapshot as gs
import infrastructure


def git(repo, *args):
    done = subprocess.run(
        ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@e", "-c", "protocol.file.allow=always", *args],
        capture_output=True, text=True,
    )
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


def write(path, text="x\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)


def host_elf():
    machine = {v: k for k, v in infrastructure.CFFI_ELF_MACHINES.items()}[infrastructure.cffi_host_machine()]
    return b"\x7fELF" + bytes([2, 1, 1]) + bytes(11) + struct.pack("<H", machine) + bytes(8)


class Workspace:
    """workspace/{hams_com, hams_open, hams_open/hams_shared}, each pushed to its own bare origin."""

    def __init__(self, root):
        self.root = root
        self.origins = {}
        for name, path in (("shared", "hams_open/hams_shared"), ("open", "hams_open"), ("com", "hams_com")):
            self.origins[name] = os.path.join(root, "origins", name + ".git")
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origins[name]], check=True)
        self.path = lambda *p: os.path.join(root, *p)
        write(self.path("hams_open/hams_shared/tools/a.py"))
        self.init("hams_open/hams_shared", "shared")
        write(self.path("hams_open/mod_open/__manifest__.py"), "{}\n")
        write(self.path("hams_open/daemons/cloudflared-ffi/main.go"), "package main\n")
        write(self.path("hams_open/daemons/cloudflared-ffi/go.mod"), "module x\ngo 1.22\n")
        self.init("hams_open", "open", extra=["hams_shared"])
        write(self.path("hams_com/mod_com/__manifest__.py"), "{}\n")
        write(self.path("hams_com/other/__manifest__.py"), "{}\n")
        write(self.path("hams_com/daemons/hams_local_relay/src.rs"), "fn main(){}\n")
        write(self.path("hams_com/daemons/shack_console/console/page.js"), "1\n")
        os.symlink("../hams_open/hams_shared", self.path("hams_com/hams_shared"))
        self.init("hams_com", "com")

    def init(self, relative, name, extra=()):
        repo = self.path(relative)
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
        git(repo, "add", "-A", ".") if not extra else git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "c")
        git(repo, "remote", "add", "origin", self.origins[name])
        git(repo, "push", "-q", "origin", "main")
        git(repo, "fetch", "-q", "origin", "main")

    def advance(self, relative, name, filename="new.txt"):
        repo = self.path(relative)
        write(os.path.join(repo, filename), str(time.time()))
        git(repo, "add", filename)
        git(repo, "commit", "-q", "-m", "more")
        git(repo, "push", "-q", "origin", "main")
        return git(repo, "rev-parse", "HEAD")


def fake_run(command, cwd, env):
    """Stands in for build_cloudflared_ffi.py and cargo: writes the outputs the real commands would."""
    if "--ffi-dir" in command:
        with open(os.path.join(command[command.index("--ffi-dir") + 1], "libcloudflared.so"), "wb") as handle:
            handle.write(host_elf())
    else:
        target = os.path.join(env["CARGO_TARGET_DIR"], "debug")
        os.makedirs(target, exist_ok=True)
        binary = os.path.join(target, "hams_local_relay")
        write(binary, "#!/bin/sh\n")
        os.chmod(binary, 0o755)
    return 0


class SnapshotTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(self.tmp.name)
        self.cargo = os.path.join(self.tmp.name, "cargo-target")
        self.out = []

    def create(self, **kwargs):
        kwargs.setdefault("build", True)
        return gs.create_snapshot(
            self.ws.root, os.path.join(self.tmp.name, "gates"), 2, run=fake_run, out=self.out.append, hostname="devbox",
            cargo_target_dir=self.cargo, **kwargs
        )

    def env(self, manifest):
        return {gs.OPEN_TREE_ENV: manifest["trees"]["open"]}


class CommitTests(SnapshotTestBase):
    # Tests [@ANCHOR: gate_snapshot:refuse_production]
    def test_the_production_host_is_refused(self):
        for name in ("hams1", "hams1.example"):
            with self.assertRaises(gs.SnapshotError):
                gs.refuse_production(name, ())
        with tempfile.NamedTemporaryFile() as marker, self.assertRaises(gs.SnapshotError):
            gs.refuse_production("devbox", (marker.name,))
        self.assertIsNone(gs.refuse_production("devbox", ()))

    # Tests [@ANCHOR: gate_snapshot:origin_main]
    # Tests [@ANCHOR: gate_snapshot:recorded_shared_commit]
    # Tests [@ANCHOR: gate_snapshot:gate_dir_name]
    def test_origin_main_follows_the_origin_and_the_gitlink_names_the_shared_commit(self):
        com = self.ws.path("hams_com")
        before = gs.origin_main(com)
        self.assertEqual(before, git(com, "rev-parse", "HEAD"))
        other = tempfile.mkdtemp(dir=self.tmp.name)
        subprocess.run(["git", "clone", "-q", self.ws.origins["com"], other], check=True)
        write(os.path.join(other, "z.txt"))
        git(other, "add", "z.txt")
        git(other, "commit", "-q", "-m", "elsewhere")
        git(other, "push", "-q", "origin", "main")
        self.assertEqual(gs.origin_main(com, fetch=False), before)
        self.assertNotEqual(gs.origin_main(com, fetch=True), before)
        open_commit = gs.origin_main(self.ws.path("hams_open"))
        self.assertEqual(
            gs.recorded_shared_commit(self.ws.path("hams_open"), open_commit), git(self.ws.path("hams_open/hams_shared"), "rev-parse", "HEAD")
        )
        with self.assertRaises(gs.SnapshotError):
            gs.recorded_shared_commit(self.ws.path("hams_com"), git(com, "rev-parse", "HEAD"))
        self.assertEqual(gs.gate_dir_name("a" * 40, "b" * 40), "gate_aaaaaaaaa_bbbbbbbbb")


class CreateTests(SnapshotTestBase):
    # Tests [@ANCHOR: gate_snapshot:create_snapshot]
    # Tests [@ANCHOR: gate_snapshot:create_trees]
    # Tests [@ANCHOR: gate_snapshot:discover_modules]
    # Tests [@ANCHOR: gate_snapshot:shard_command]
    def test_create_makes_three_exact_trees_builds_and_verifies_and_prints_the_commands(self):
        manifest = self.create()
        trees = manifest["trees"]
        self.assertEqual(git(trees["com"], "rev-parse", "HEAD"), git(self.ws.path("hams_com"), "rev-parse", "origin/main"))
        self.assertEqual(git(trees["open"], "rev-parse", "HEAD"), git(self.ws.path("hams_open"), "rev-parse", "origin/main"))
        self.assertEqual(git(trees["shared"], "rev-parse", "HEAD"), trees["shared_commit"])
        self.assertEqual(os.path.realpath(os.path.join(trees["com"], "hams_shared")), os.path.realpath(trees["shared"]))
        self.assertEqual(sorted(m for s in manifest["shards"] for m in s), ["mod_com", "mod_open", "other"])
        with open(os.path.join(trees["parent"], "gate_shards.json")) as handle:
            self.assertEqual(json.load(handle), manifest["shards"])
        command = manifest["commands"][0]
        for part in ("nice -n 10", "HAMS_OPEN_TREE=" + trees["open"], "--wait", "HAMS_TEST_BATCH=off", "-u "):
            self.assertIn(part, command)
        for stale in ("sudo", "HAMS_ISOLATED_NS", "HAMS_CI_LOAD_GATE_RATIO", "--solo"):
            self.assertNotIn(stale, command)
        self.assertEqual(gs.verify_snapshot(trees["parent"], environ=self.env(manifest)), [])
        self.assertTrue(os.path.isfile(os.path.join(trees["open"], "daemons/cloudflared-ffi/libcloudflared.so")))

    # Tests [@ANCHOR: gate_snapshot:create_snapshot]
    def test_a_second_create_for_the_same_commits_reuses_the_trees_and_builds_nothing(self):
        self.create()
        built = []
        gs.create_snapshot(
            self.ws.root, os.path.join(self.tmp.name, "gates"), 2, run=lambda *a: built.append(a) or 0, out=lambda *_: None,
            hostname="devbox", cargo_target_dir=self.cargo,
        )
        self.assertEqual(built, [])

    # Tests [@ANCHOR: gate_snapshot:create_snapshot]
    def test_create_refuses_on_the_production_host_and_for_a_missing_build(self):
        with self.assertRaises(gs.SnapshotError):
            gs.create_snapshot(self.ws.root, self.tmp.name, hostname="hams1")
        with self.assertRaises(gs.SnapshotError):  # a build that exits 0 but writes nothing must not pass
            gs.create_snapshot(
                self.ws.root, os.path.join(self.tmp.name, "gates"), 2, run=lambda *a: 0, out=lambda *_: None,
                hostname="devbox", cargo_target_dir=self.cargo,
            )

    # Tests [@ANCHOR: gate_snapshot:create_snapshot]
    def test_create_reads_a_previous_shard_file_and_refuses_a_malformed_one(self):
        shards = os.path.join(self.tmp.name, "prev.json")
        write(shards, json.dumps([["mod_com", "gone"], ["other"]]))
        manifest = self.create(shards_file=shards)
        self.assertEqual(manifest["shards"][0][0], "mod_com")
        self.assertNotIn("gone", [m for s in manifest["shards"] for m in s])
        write(shards, "{}")
        with self.assertRaises(gs.SnapshotError):
            self.create(shards_file=shards)

    # Tests [@ANCHOR: gate_snapshot:make_shards]
    def test_shards_cover_every_module_once_and_keep_old_placement(self):
        modules = ["a", "b", "c", "d", "e"]
        dealt = gs.make_shards(modules, 2)
        self.assertEqual(sorted(m for s in dealt for m in s), modules)
        kept = gs.make_shards(modules, 2, [["a", "x"], ["b"]])
        self.assertEqual(kept[0][0], "a")
        self.assertEqual(sorted(m for s in kept for m in s), modules)
        self.assertEqual(gs.make_shards(["a"], 3), [["a"]])

    # Tests [@ANCHOR: gate_snapshot:create_trees]
    def test_a_missing_commit_is_refused(self):
        with self.assertRaises(gs.SnapshotError):
            gs.create_trees(self.ws.root, self.tmp.name, "1" * 40, git(self.ws.path("hams_open"), "rev-parse", "HEAD"), "2" * 40)


class VerifyTests(SnapshotTestBase):
    def setUp(self):
        super().setUp()
        self.manifest = self.create()
        self.trees = self.manifest["trees"]
        self.env_ = self.env(self.manifest)

    def problems(self, **kwargs):
        return gs.verify_snapshot(self.trees["parent"], environ=kwargs.pop("environ", self.env_), **kwargs)

    # Tests [@ANCHOR: gate_snapshot:verify_snapshot]
    def test_a_stale_hams_open_checkout_is_refused(self):
        self.ws.advance("hams_open", "open")
        problems = self.problems()
        self.assertTrue(any("open is behind origin/main" in p for p in problems), problems)

    # Tests [@ANCHOR: gate_snapshot:verify_snapshot]
    def test_a_stale_hams_com_and_a_stale_shared_commit_are_refused(self):
        self.ws.advance("hams_com", "com")
        self.assertTrue(any("com is behind origin/main" in p for p in self.problems()))

    # Tests [@ANCHOR: gate_snapshot:verify_snapshot]
    def test_a_moved_origin_is_not_seen_without_a_fetch_and_is_with_one(self):
        other = tempfile.mkdtemp(dir=self.tmp.name)
        subprocess.run(["git", "clone", "-q", self.ws.origins["com"], other], check=True)
        write(os.path.join(other, "z.txt"))
        git(other, "add", "z.txt")
        git(other, "commit", "-q", "-m", "elsewhere")
        git(other, "push", "-q", "origin", "main")
        self.assertEqual(self.problems(fetch=False), [])
        self.assertTrue(any("com is behind" in p for p in self.problems(fetch=True)))

    # Tests [@ANCHOR: gate_snapshot:verify_snapshot]
    def test_open_tree_must_be_set_and_name_the_gate(self):
        self.assertTrue(any("HAMS_OPEN_TREE is not set" in p for p in self.problems(environ={})))
        self.assertTrue(any("not the gate's hams_open" in p for p in self.problems(environ={gs.OPEN_TREE_ENV: self.ws.path("hams_open")})))

    # Tests [@ANCHOR: gate_snapshot:verify_snapshot]
    def test_an_edited_tree_a_moved_head_and_an_unknown_shard_module_are_refused(self):
        write(os.path.join(self.trees["com"], "mod_com", "__manifest__.py"), "{'edited': 1}\n")
        self.assertTrue(any("uncommitted changes" in p for p in self.problems()))
        git(self.trees["com"], "checkout", "-q", "--", ".")
        git(self.trees["com"], "commit", "-q", "--allow-empty", "-m", "moved")
        self.assertTrue(any("is at" in p for p in self.problems()))
        git(self.trees["com"], "reset", "-q", "--hard", self.trees["com_commit"])
        manifest_path = os.path.join(self.trees["parent"], gs.MANIFEST_NAME)
        with open(manifest_path) as handle:
            data = json.load(handle)
        data["shards"].append(["no_such_module"])
        write(manifest_path, json.dumps(data))
        self.assertTrue(any("no_such_module has no manifest" in p for p in self.problems()))

    # Tests [@ANCHOR: gate_snapshot:verify_snapshot]
    def test_a_missing_manifest_is_an_error(self):
        with self.assertRaises(gs.SnapshotError):
            gs.verify_snapshot(self.tmp.name)


class ArtifactTests(SnapshotTestBase):
    def setUp(self):
        super().setUp()
        self.manifest = self.create()
        self.trees = gs.Trees(**self.manifest["trees"])
        self.lib = os.path.join(self.trees.open, "daemons/cloudflared-ffi/libcloudflared.so")
        self.relay = os.path.join(self.cargo, "debug", "hams_local_relay")

    def problems(self, environ=None):
        out = []
        for artifact in gs.ARTIFACTS:
            out += gs.artifact_problems(artifact, self.trees, self.cargo, environ or {})
        return out

    # Tests [@ANCHOR: gate_snapshot:artifact_output]
    # Tests [@ANCHOR: gate_snapshot:source_stamp]
    # Tests [@ANCHOR: gate_snapshot:build_artifact]
    def test_outputs_have_the_documented_paths_and_a_stamp_of_the_current_sources(self):
        self.assertEqual(gs.artifact_output(gs.ARTIFACTS[0], self.trees, self.cargo), self.lib)
        self.assertEqual(gs.artifact_output(gs.ARTIFACTS[1], self.trees, self.cargo), self.relay)
        stamp = gs.source_stamp(gs.ARTIFACTS[1], self.trees)
        self.assertEqual(stamp[gs.CONSOLE_DIR], git(self.trees.com, "rev-parse", "HEAD:daemons/shack_console/console"))
        with open(self.lib + gs.STAMP_SUFFIX) as handle:
            self.assertEqual(json.load(handle)["sources"], gs.source_stamp(gs.ARTIFACTS[0], self.trees))
        with open(self.relay + ".console_tree") as handle:
            self.assertEqual(handle.read().strip(), stamp[gs.CONSOLE_DIR])

    # Tests [@ANCHOR: gate_snapshot:build_artifact]
    def test_a_failing_build_command_is_an_error(self):
        with self.assertRaises(gs.SnapshotError):
            gs.build_artifact(gs.ARTIFACTS[0], self.trees, self.cargo, run=lambda *a: 1, out=lambda *_: None)

    # Tests [@ANCHOR: gate_snapshot:artifact_problems]
    def test_fresh_outputs_have_no_problems(self):
        self.assertEqual(self.problems({gs.RELAY_BIN_ENV: self.relay}), [])

    # Tests [@ANCHOR: gate_snapshot:artifact_problems]
    def test_a_relay_built_before_a_change_in_a_crate_it_links_is_refused(self):
        write(os.path.join(self.trees.com, "daemons/shack_console/hams_core/lib.rs"), "fn changed(){}\n")
        git(self.trees.com, "add", "daemons/shack_console/hams_core/lib.rs")
        git(self.trees.com, "commit", "-q", "-m", "core change")
        self.assertTrue(any("no stamp" in p for p in self.problems()), self.problems())

    # Tests [@ANCHOR: gate_snapshot:artifact_problems]
    def test_a_library_with_an_old_timestamp_copied_over_is_refused(self):
        old = time.time() - 86400 * 30
        os.utime(self.lib, (old, old))
        problems = self.problems()
        self.assertTrue(any("older than" in p for p in problems), problems)

    # Tests [@ANCHOR: gate_snapshot:artifact_problems]
    def test_a_library_without_a_stamp_or_for_other_sources_is_refused(self):
        os.unlink(self.lib + gs.STAMP_SUFFIX)
        self.assertTrue(any("no stamp" in p for p in self.problems()))

    # Tests [@ANCHOR: gate_snapshot:artifact_problems]
    def test_a_missing_or_wrong_cpu_library_and_a_missing_relay_are_refused(self):
        with open(self.lib, "wb") as handle:
            handle.write(b"not an elf")
        self.assertTrue(any("not a readable ELF" in p for p in self.problems()))
        os.unlink(self.lib)
        self.assertTrue(any("does not exist" in p for p in self.problems()))
        os.unlink(self.relay)
        self.assertTrue(sum("does not exist" in p for p in self.problems()) == 2)

    # Tests [@ANCHOR: gate_snapshot:artifact_problems]
    def test_a_relay_built_from_another_console_or_named_wrongly_is_refused(self):
        write(self.relay + ".console_tree", "0" * 40 + "\n")
        self.assertTrue(any("another console tree" in p for p in self.problems()))
        self.assertTrue(any("is not the gate's binary" in p for p in self.problems({gs.RELAY_BIN_ENV: "/somewhere/else"})))
        os.chmod(self.relay, 0o644)
        self.assertTrue(any("not executable" in p for p in self.problems()))


class PruneAndCommandTests(SnapshotTestBase):
    # Tests [@ANCHOR: gate_snapshot:prune_snapshots]
    def test_prune_removes_old_gate_trees_through_git_and_nothing_else(self):
        first = self.create()["trees"]["parent"]
        os.utime(first, (time.time() - 1000, time.time() - 1000))
        self.ws.advance("hams_com", "com")
        second = self.create()["trees"]["parent"]
        keep_me = os.path.join(self.tmp.name, "gates", "not_a_gate")
        os.makedirs(keep_me)
        removed = gs.prune_snapshots(os.path.join(self.tmp.name, "gates"), keep=1, out=lambda *_: None)
        self.assertEqual(removed, [first])
        self.assertTrue(os.path.isdir(second) and os.path.isdir(keep_me) and not os.path.exists(first))
        self.assertNotIn(first, git(self.ws.path("hams_com"), "worktree", "list"))

    # Tests [@ANCHOR: gate_snapshot:prune_snapshots]
    def test_prune_unregisters_a_worktree_whose_repository_keeps_its_git_directory_elsewhere(self):
        # hams_shared is a submodule: its common directory is not `<repo>/.git`, so dirname(common dir) is not the repository.
        parent = os.path.join(self.tmp.name, "prune_parent")
        repo = os.path.join(parent, "repo")
        os.makedirs(repo)
        os.makedirs(os.path.join(parent, "modules"))
        separate = os.path.join(parent, "modules", "repo.git")
        git(repo, "init", "-q", "--separate-git-dir", separate)
        git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "x")
        gate = os.path.join(parent, "gate_old")
        os.makedirs(os.path.join(gate, "hams_open"))
        git(repo, "worktree", "add", "-q", "--detach", os.path.join(gate, "hams_open", "hams_shared"))
        os.utime(gate, (time.time() - 1000, time.time() - 1000))
        newer = os.path.join(parent, "gate_new")
        write(os.path.join(newer, gs.MANIFEST_NAME), "{}")
        self.assertEqual(gs.prune_snapshots(parent, keep=1, out=lambda *_: None), [gate])
        self.assertNotIn("gate_old", git(repo, "worktree", "list"))

    # Tests [@ANCHOR: gate_snapshot:prune_snapshots]
    def test_prune_leaves_a_gate_named_directory_that_is_not_a_snapshot(self):
        parent = os.path.join(self.tmp.name, "prune_parent2")
        notes = os.path.join(parent, "gate_notes")
        write(os.path.join(notes, "keep.txt"), "mine")
        os.utime(notes, (time.time() - 1000, time.time() - 1000))
        write(os.path.join(parent, "gate_new", gs.MANIFEST_NAME), "{}")
        messages = []
        self.assertEqual(gs.prune_snapshots(parent, keep=1, out=messages.append), [])
        self.assertTrue(os.path.exists(os.path.join(notes, "keep.txt")))
        self.assertTrue(any("not touching" in m for m in messages))

    # Tests [@ANCHOR: gate_snapshot:create_snapshot]
    def test_the_shard_log_directories_are_not_world_writable(self):
        manifest = self.create()
        for number in range(1, len(manifest["shards"]) + 1):
            mode = os.stat(os.path.join(manifest["trees"]["parent"], "logs", "shard%d" % number)).st_mode & 0o777
            self.assertEqual(mode, 0o755)

    # Tests [@ANCHOR: gate_snapshot:main]
    def test_main_creates_verifies_refuses_and_lists_commands(self):
        out = []
        base = ["--workspace", self.ws.root, "--parent", os.path.join(self.tmp.name, "gates"), "--cargo-target-dir", self.cargo]
        with unittest.mock.patch.object(gs.subprocess, "call", side_effect=lambda command, **k: fake_run(command, k.get("cwd"), k.get("env")) if command[0] != "nice" else fake_run(command[2:], k.get("cwd"), k.get("env"))):
            self.assertEqual(gs.main(["create", *base], out.append), 0)
        gate = [line for line in out if "snapshot ready" in line][0].split(" in ")[1].split(" (")[0]
        self.assertEqual(gs.main(["commands", gate], out.append), 0)
        self.assertTrue(out[-1].startswith("cd "))
        with unittest.mock.patch.dict(os.environ, {gs.OPEN_TREE_ENV: os.path.join(gate, "hams_open")}):
            self.assertEqual(gs.main(["verify", gate], out.append), 0)
            self.ws.advance("hams_open", "open")
            out.clear()
            self.assertEqual(gs.main(["verify", gate], out.append), 1)
        self.assertEqual(out[0], "REFUSING: the gate snapshot is stale or incomplete:")
        self.assertTrue(any("open is behind origin/main" in line for line in out))


class RunnerFlagTests(unittest.TestCase):
    # Tests [@ANCHOR: gate_snapshot:main]
    def test_the_runner_offers_snapshot_gate(self):
        done = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "test.py"), "--help"],
                              capture_output=True, text=True)
        self.assertIn("--snapshot-gate", done.stdout)


if __name__ == "__main__":
    unittest.main()
