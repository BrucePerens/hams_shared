#!/usr/bin/env python3
# This file is part of hams_open, an open source module.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the libcloudflared.so build in infrastructure.py, build_cloudflared_ffi.py,
the provisioning wiring and test.py's preflight.

Tests [@ANCHOR: infrastructure:build_cloudflared_ffi]
Tests [@ANCHOR: infrastructure:hook_build_cloudflared_ffi]
Tests [@ANCHOR: test:check_cloudflared_ffi_library]

The build test runs the real Go toolchain on the real in-repo source, offline: a test host
without `golang-1.24-go` fails here on purpose, the same way it fails the cloudflare module's
tunnel tests, so the missing environment is found by the one test that names it.
"""
import ctypes
import importlib.util
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

import infrastructure as infra


def _load_test_py_module():
    # test.py is loaded by path: `import test` would resolve to the standard library's package.
    spec = importlib.util.spec_from_file_location(
        "_hams_test_runner_for_ffi_preflight", os.path.join(os.path.dirname(__file__), "test.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


test_runner = _load_test_py_module()


def _elf_header(machine):
    return b"\x7fELF\x02\x01\x01" + b"\x00" * 11 + struct.pack("<H", machine) + b"\x00" * 8


def _write_lib(directory, machine):
    path = os.path.join(directory, infra.CFFI_LIB_NAME)
    with open(path, "wb") as f:
        f.write(_elf_header(machine))
    return path


def _other_machine_code():
    return 183 if infra.cffi_host_machine() == "x86_64" else 62


def _this_machine_code():
    return {v: k for k, v in infra.CFFI_ELF_MACHINES.items()}[infra.cffi_host_machine()]


class LibraryStatusTests(unittest.TestCase):
    def test_missing_library(self):
        with tempfile.TemporaryDirectory() as d:
            ok, reason = infra.cffi_library_status(d)
        self.assertFalse(ok)
        self.assertIn("does not exist", reason)

    def test_wrong_architecture_is_not_ok(self):
        with tempfile.TemporaryDirectory() as d:
            _write_lib(d, _other_machine_code())
            ok, reason = infra.cffi_library_status(d)
        self.assertFalse(ok)
        self.assertIn("was built for", reason)

    def test_right_architecture_is_ok_and_older_source_is_not_newer(self):
        with tempfile.TemporaryDirectory() as d:
            lib = _write_lib(d, _this_machine_code())
            self.assertEqual(infra.cffi_library_status(d), (True, ""))
            src = os.path.join(d, "main.go")
            open(src, "w").close()
            os.utime(src, (os.path.getmtime(lib) + 10,) * 2)
            ok, reason = infra.cffi_library_status(d)
        self.assertFalse(ok)
        self.assertIn("older than main.go", reason)

    def test_non_elf_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, infra.CFFI_LIB_NAME), "wb") as f:
                f.write(b"not an elf file at all, just text")
            ok, reason = infra.cffi_library_status(d)
        self.assertFalse(ok)
        self.assertIn("not a readable ELF", reason)


class CommandLineTests(unittest.TestCase):
    def test_check_reports_missing_and_exits_one(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_cloudflared_ffi.py")
        with tempfile.TemporaryDirectory() as d:
            res = subprocess.run([sys.executable, script, "--check", "--ffi-dir", d],
                                 capture_output=True, text=True, check=False)
        self.assertEqual(res.returncode, 1)
        self.assertIn("MISSING", res.stdout)


class FfiSourceIsOfflineBuildableTests(unittest.TestCase):
    """The source tree must stay buildable with the apt Go and no network."""

    FFI_DIR = infra.cloudflared_ffi_default_dir()

    def test_go_mod_needs_no_toolchain_download_and_no_dependencies(self):
        with open(os.path.join(self.FFI_DIR, "go.mod"), encoding="utf-8") as f:
            go_mod = f.read()
        self.assertNotIn("toolchain", go_mod)
        self.assertNotRegex(go_mod, r"(?m)^\s*require\b")
        self.assertLessEqual(infra.cffi_required_go(self.FFI_DIR), (1, 24), "apt ships golang-1.24-go")
        with open(os.path.join(self.FFI_DIR, "go.sum"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "", "a dependency would need a module download")

    def test_build_environment_cannot_reach_a_network(self):
        env = infra.cffi_build_env({"PATH": "/usr/bin", "GOFLAGS": "-mod=mod", "GOPROXY": "https://x"}, "/c")
        self.assertEqual(env["GOTOOLCHAIN"], "local")
        self.assertEqual(env["GOPROXY"], "off")
        self.assertEqual(env["GOFLAGS"], "-mod=readonly")

    def test_library_is_not_tracked_in_git(self):
        # An x86-64 binary in git is what broke arm64 test hosts. The directory's .gitignore line
        # lives in hams_open, so look for it relative to the tree this file is in.
        gitignore = os.path.join(self.FFI_DIR, "..", "..", ".gitignore")
        with open(gitignore, encoding="utf-8") as f:
            self.assertIn("daemons/cloudflared-ffi/libcloudflared.so", f.read())

    def test_real_build_produces_a_loadable_library_for_this_cpu(self):
        with tempfile.TemporaryDirectory() as work:
            for name in ("main.go", "go.mod", "go.sum"):
                shutil.copy(os.path.join(self.FFI_DIR, name), work)
            lib = infra.cffi_build(work)
            self.assertEqual(infra.cffi_elf_machine(lib), infra.cffi_host_machine())
            self.assertEqual(infra.cffi_library_status(work), (True, ""))
            loaded = ctypes.CDLL(lib)
            self.assertIsNotNone(loaded.StartLocalSimulator)  # AttributeError if not exported
            self.assertIsNotNone(loaded.StopLocalSimulator)


class ProvisioningWiringTests(unittest.TestCase):
    """Classification: provisioning must give a test host everything the build needs."""

    def test_golang_package_is_in_the_apt_manifest(self):
        names = {p["name"] for p in infra.MANIFEST["apt_packages"]}
        self.assertIn(infra.CFFI_APT_PACKAGE, names)
        self.assertIn("build-essential", names, "cgo needs a C compiler")

    def test_daemons_entry_runs_the_build_hook_before_the_perms_fixup(self):
        hooks = [
            h for f in infra.MANIFEST["static_files"] for h in f.get("post_provision_hooks", [])
            if infra.hook_build_rust_daemons in f.get("post_provision_hooks", [])
        ]
        self.assertIn(infra.hook_build_cloudflared_ffi, hooks)
        self.assertLess(hooks.index(infra.hook_build_cloudflared_ffi), hooks.index(infra.hook_daemons_perms))

    def test_hook_builds_into_the_community_tree_and_records_failure(self):
        with tempfile.TemporaryDirectory() as community:
            ffi_dir = os.path.join(community, "daemons", "cloudflared-ffi")
            os.makedirs(ffi_dir)
            with open(os.path.join(ffi_dir, "main.go"), "w") as f:
                f.write("package main\nfunc main() {}\n")  # no exports, no go.mod: must fail
            infra.reset_hook_failures()
            infra.hook_build_cloudflared_ffi({"HAMS_COMMUNITY_DIR": community}, "", "/x", None)
            failures = infra.get_hook_failures()
            infra.reset_hook_failures()
        self.assertEqual([name for name, _ in failures], ["hook_build_cloudflared_ffi"])

    def test_hook_without_a_community_dir_does_nothing(self):
        infra.reset_hook_failures()
        infra.hook_build_cloudflared_ffi({}, "", "/x", None)
        self.assertEqual(infra.get_hook_failures(), [])


class PreflightTests(unittest.TestCase):
    def _addons(self, root):
        os.makedirs(os.path.join(root, "cloudflare"))
        open(os.path.join(root, "cloudflare", "__manifest__.py"), "w").close()
        os.makedirs(os.path.join(root, "daemons", "cloudflared-ffi"))
        return root

    def test_missing_library_gives_one_message_with_the_fix(self):
        with tempfile.TemporaryDirectory() as root:
            msg = test_runner.check_cloudflared_ffi_library(self._addons(root), ["cloudflare"])
        self.assertIn("Missing libcloudflared.so", msg)
        self.assertIn("build_cloudflared_ffi.py", msg)
        self.assertIn(infra.CFFI_APT_PACKAGE, msg)

    def test_other_modules_are_not_checked(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(test_runner.check_cloudflared_ffi_library(self._addons(root), ["ham_shack"]))

    def test_present_library_passes(self):
        with tempfile.TemporaryDirectory() as root:
            addons = self._addons(root)
            _write_lib(os.path.join(root, "daemons", "cloudflared-ffi"), _this_machine_code())
            self.assertIsNone(test_runner.check_cloudflared_ffi_library(addons, ["cloudflare", "x"]))

    def test_message_names_a_command_that_exists(self):
        with tempfile.TemporaryDirectory() as root:
            msg = test_runner.check_cloudflared_ffi_library(self._addons(root), ["cloudflare"])
        script = re.search(r"python3 (\S+build_cloudflared_ffi\.py)", msg).group(1)
        self.assertTrue(os.path.exists(script))


if __name__ == "__main__":
    unittest.main()
