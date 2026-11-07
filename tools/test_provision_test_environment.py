#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for provision_test_environment.py (`provision.py --test-environment`).

The pure decisions (credential order, production refusal, package selection, profile contents) are tested directly.
The repository step runs real `git clone` against throwaway local origins, so "clone only what is missing, never touch
an existing checkout" is checked with git itself. Steps that would change this host (apt, pip, systemctl, psql) are
driven with a recording `run` and patched read-only probes. No real key is used anywhere: the SSH key tests use an
empty file whose mode is set by the test.
"""
import base64
import importlib.metadata
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import infrastructure
import provision
import provision_test_environment as pte


GIT = ["git", "-c", "commit.gpgsign=false", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def _git(*args, cwd=None):
    return subprocess.run(GIT + list(args), cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Recorder:
    """A stand-in for provision.py's run_sys: records argv and succeeds."""

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _real_run(cmd, **kw):
    kw.pop("env", None)
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def _quiet(func, *args, **kwargs):
    with redirect_stdout(io.StringIO()):
        return func(*args, **kwargs)


class ProductionRefusalTests(unittest.TestCase):
    # Tests [@ANCHOR: provision_test_environment:production_refusal_reasons]
    # Tests [@ANCHOR: provision_test_environment:refuse_production]
    # Tests [@ANCHOR: provision_test_environment:production_markers]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_no_markers_and_no_hams_prod_is_not_production(self):
        markers = (os.path.join(self.tmp, "DEPLOY_LOG"), os.path.join(self.tmp, "DEPLOYED_COMMITS"))
        self.assertEqual(pte.production_refusal_reasons(markers, ("postgres", "hams_test")), [])
        pte.refuse_production(markers, ("hams_test",))  # returns, no exit

    def test_each_marker_file_alone_refuses_with_exit_2(self):
        for name in ("DEPLOY_LOG", "DEPLOYED_COMMITS"):
            marker = os.path.join(self.tmp, name)
            open(marker, "w").close()
            with self.assertRaises(SystemExit) as caught:
                _quiet(pte.refuse_production, (marker,), ())
            self.assertEqual(caught.exception.code, 2)
            os.unlink(marker)

    def test_a_hams_prod_database_refuses(self):
        with self.assertRaises(SystemExit) as caught:
            _quiet(pte.refuse_production, (), ("postgres", "hams_prod"))
        self.assertEqual(caught.exception.code, 2)

    def test_the_markers_are_the_ones_test_py_refuses_on(self):
        import test as test_runner
        self.assertEqual(pte.PRODUCTION_MARKER_FILES, test_runner.PRODUCTION_HOST_MARKER_FILES)
        self.assertEqual(pte.PRODUCTION_DATABASE_NAME, test_runner.PRODUCTION_DATABASE_NAME)

    def test_refuse_production_reads_the_module_markers_at_call_time(self):
        marker = os.path.join(self.tmp, "DEPLOY_LOG")
        open(marker, "w").close()
        with patch.object(pte, "PRODUCTION_MARKER_FILES", (marker,)):
            with self.assertRaises(SystemExit):
                _quiet(pte.refuse_production)


class ListDatabaseNamesTests(unittest.TestCase):
    # Tests [@ANCHOR: provision_test_environment:list_database_names]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_no_server_socket_means_no_databases_and_no_query(self):
        runner = MagicMock()
        self.assertEqual(pte.list_database_names(self.tmp, runner), ())
        runner.assert_not_called()

    def test_running_server_is_listed(self):
        open(os.path.join(self.tmp, ".s.PGSQL.5432"), "w").close()
        runner = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout="postgres\nhams_prod\n", stderr=""))
        self.assertEqual(pte.list_database_names(self.tmp, runner), ("postgres", "hams_prod"))

    def test_running_server_that_cannot_be_listed_exits_2(self):
        open(os.path.join(self.tmp, ".s.PGSQL.5432"), "w").close()
        runner = MagicMock(return_value=subprocess.CompletedProcess([], 2, stdout="", stderr="no"))
        with self.assertRaises(SystemExit) as caught:
            _quiet(pte.list_database_names, self.tmp, runner)
        self.assertEqual(caught.exception.code, 2)


class ProvisionPyTestEnvironmentFlagTests(unittest.TestCase):
    """provision.py's own handling of the flag: the refusal comes before the sudo re-exec and anything else."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_production_refusal_happens_before_the_sudo_reexec(self):
        marker = os.path.join(self.tmp, "DEPLOYED_COMMITS")
        open(marker, "w").close()
        with patch.object(pte, "PRODUCTION_MARKER_FILES", (marker,)), \
                patch.object(sys, "argv", ["provision.py", "--test-environment"]), \
                patch.object(os, "geteuid", return_value=1000), \
                patch.object(os, "execvp", side_effect=AssertionError("re-exec reached")) as execvp, \
                patch("os.chdir"):
            with self.assertRaises(SystemExit) as caught:
                _quiet(provision.provision)
        self.assertEqual(caught.exception.code, 2)
        execvp.assert_not_called()

    def test_git_key_without_test_environment_is_refused(self):
        with patch.object(sys, "argv", ["provision.py", "--git-key", "/x"]), \
                patch.object(os, "geteuid", return_value=0), patch("os.chdir"), \
                patch.object(provision, "repo_root", self.tmp), \
                patch.object(provision.infrastructure, "get_os_identifier", return_value="debian"):
            with self.assertRaises(SystemExit) as caught:
                provision.provision()
        self.assertEqual(caught.exception.code, 1)


class CredentialTests(unittest.TestCase):
    # Tests [@ANCHOR: provision_test_environment:choose_credentials]
    # Tests [@ANCHOR: provision_test_environment:check_git_key]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.key = os.path.join(self.tmp, "deploy key")
        open(self.key, "w").close()  # an empty stand-in, never a real key
        os.chmod(self.key, 0o600)

    def test_cloud_proxy_comes_first(self):
        chosen = pte.choose_credentials({"CLAUDE_CODE_REMOTE": "true"}, True, self.key)
        self.assertEqual(chosen["method"], "cloud_proxy")
        self.assertTrue(chosen["url_format"].startswith("https://"))

    def test_gh_comes_before_the_key(self):
        self.assertEqual(pte.choose_credentials({}, True, self.key)["method"], "gh")

    def test_key_when_neither_cloud_nor_gh(self):
        chosen = pte.choose_credentials({}, False, self.key)
        self.assertEqual(chosen["method"], "ssh_key")
        self.assertEqual(chosen["url_format"], "git@github.com:{owner}/{repo}.git")
        self.assertIn("'" + self.key + "'", chosen["ssh_command"])  # the space in the path is quoted
        self.assertIn("IdentitiesOnly=yes", chosen["ssh_command"])

    def test_nothing_means_read_only_https_and_says_so(self):
        chosen = pte.choose_credentials({"CLAUDE_CODE_REMOTE": "false"}, False, None)
        self.assertEqual(chosen["method"], "read_only")
        self.assertIn("pushing is not set up", chosen["note"])

    def test_key_with_another_mode_is_refused(self):
        for mode in (0o644, 0o640, 0o400, 0o700):
            os.chmod(self.key, mode)
            with self.assertRaisesRegex(ValueError, "must be 600"):
                pte.choose_credentials({}, False, self.key)

    def test_missing_key_and_directory_are_refused(self):
        with self.assertRaises(ValueError):
            pte.check_git_key(os.path.join(self.tmp, "absent"))
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            pte.check_git_key(self.tmp)

    def test_the_key_is_never_opened(self):
        real_open = open

        def guarded_open(path, *args, **kwargs):
            if os.path.abspath(str(path)) == self.key:
                raise AssertionError("the key file was opened")
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", guarded_open):
            self.assertEqual(pte.check_git_key(self.key), self.key)


class GhAuthenticatedTests(unittest.TestCase):
    # Tests [@ANCHOR: provision_test_environment:gh_authenticated]

    def test_no_gh_binary(self):
        with patch.object(pte.shutil, "which", return_value=None):
            self.assertFalse(pte.gh_authenticated(list, MagicMock()))

    def test_status_decides(self):
        with patch.object(pte.shutil, "which", return_value="/usr/bin/gh"):
            ok = MagicMock(return_value=subprocess.CompletedProcess([], 0))
            self.assertTrue(pte.gh_authenticated(list, ok))
            ok.assert_called_once_with(["gh", "auth", "status"], capture_output=True, text=True)
            bad = MagicMock(return_value=subprocess.CompletedProcess([], 1))
            self.assertFalse(pte.gh_authenticated(list, bad))


class LayoutTests(unittest.TestCase):
    # Tests [@ANCHOR: provision_test_environment:discover_layout]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_from_inside_hams_open(self):
        hams_open = os.path.join(self.tmp, "hams_open")
        os.makedirs(os.path.join(hams_open, ".git"))
        layout = pte.discover_layout(os.path.join(hams_open, "hams_shared", "tools"), hams_open)
        self.assertEqual(layout["workspace"], self.tmp)
        self.assertEqual(layout["hams_com"], os.path.join(self.tmp, "hams_com"))
        self.assertEqual(layout["hams_shared"], os.path.join(hams_open, "hams_shared"))

    def test_from_a_standalone_hams_shared_clone(self):
        # provision._find_repo_root returns the directory that holds the clone; it is no git checkout itself.
        tools = os.path.join(self.tmp, "hams_shared", "tools")
        self.assertEqual(pte.discover_layout(tools, self.tmp)["workspace"], self.tmp)
        self.assertEqual(pte.discover_layout(tools, None)["workspace"], self.tmp)

    def test_workspace_option_wins(self):
        self.assertEqual(pte.discover_layout("/a/b/tools", "/a", workspace=self.tmp)["workspace"], self.tmp)


class RepositoryTests(unittest.TestCase):
    """Real git clones from throwaway local origins."""
    # Tests [@ANCHOR: provision_test_environment:missing_repositories]
    # Tests [@ANCHOR: provision_test_environment:provision_repositories]
    # Tests [@ANCHOR: provision_test_environment:repositories]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.origins = os.path.join(self.tmp, "origins")
        for name in pte.REPOSITORIES:
            work = os.path.join(self.tmp, "seed", name)
            os.makedirs(os.path.join(work, "hams_shared", "tools"))
            with open(os.path.join(work, "hams_shared", "tools", "test.py"), "w") as out:
                out.write("# stand-in\n")
            _git("init", "-q", "-b", "main", cwd=work)
            _git("add", ".", cwd=work)
            _git("commit", "-q", "-m", "seed", cwd=work)
            _git("clone", "-q", "--bare", work, os.path.join(self.origins, name))
        self.workspace = os.path.join(self.tmp, "ws")
        os.makedirs(self.workspace)
        self.layout = pte.discover_layout("", None, workspace=self.workspace)
        self.credentials = {"method": "cloud_proxy", "url_format": "file://" + self.origins + "/{repo}",
                            "ssh_command": None, "note": ""}

    def _provision(self, credentials=None):
        report = pte.Report()
        _quiet(pte.provision_repositories, self.layout, credentials or self.credentials, _real_run, list, report)
        return report

    def test_clones_only_what_is_missing_and_never_touches_an_existing_checkout(self):
        existing = self.layout["hams_com"]
        _git("clone", "-q", "file://" + os.path.join(self.origins, "hams_com"), existing)
        with open(os.path.join(existing, "work_in_progress.txt"), "w") as out:
            out.write("uncommitted\n")
        os.makedirs(os.path.join(existing, "hams_shared", "tools"), exist_ok=True)
        with open(os.path.join(existing, "hams_shared", "tools", "test.py"), "w") as out:
            out.write("# locally edited\n")
        head = _git("rev-parse", "HEAD", cwd=existing)
        report = self._provision()
        self.assertEqual(report.changes, [f"cloned hams_open into {self.layout['hams_open']}"])
        self.assertTrue(os.path.isdir(os.path.join(self.layout["hams_open"], ".git")))
        self.assertFalse(os.path.exists(self.layout["hams_open"] + ".partial"))
        self.assertEqual(_git("rev-parse", "HEAD", cwd=existing), head)
        with open(os.path.join(existing, "hams_shared", "tools", "test.py")) as edited:
            self.assertEqual(edited.read(), "# locally edited\n")
        self.assertTrue(os.path.exists(os.path.join(existing, "work_in_progress.txt")))

    def test_second_run_changes_nothing(self):
        self.assertEqual(len(self._provision().changes), 2)
        self.assertEqual(self._provision().changes, [])

    def test_a_leftover_partial_clone_is_replaced(self):
        os.makedirs(self.layout["hams_open"] + ".partial/junk")
        self._provision()
        self.assertTrue(os.path.isdir(os.path.join(self.layout["hams_open"], ".git")))

    def test_an_occupied_non_git_directory_is_refused(self):
        os.makedirs(self.layout["hams_com"])
        with open(os.path.join(self.layout["hams_com"], "notes.txt"), "w") as out:
            out.write("someone's\n")
        with self.assertRaisesRegex(ValueError, "not a git checkout"):
            pte.missing_repositories(self.layout)

    def test_an_empty_directory_counts_as_missing(self):
        os.makedirs(self.layout["hams_com"])
        self.assertIn(("hams_com", self.layout["hams_com"]), pte.missing_repositories(self.layout))

    def test_ssh_key_is_scoped_to_these_clones_as_core_ssh_command(self):
        credentials = dict(self.credentials, method="ssh_key", ssh_command="ssh -i /k -o IdentitiesOnly=yes")
        self._provision(credentials)
        for name in pte.REPOSITORIES:
            got = _git("config", "--local", "--get", "core.sshCommand", cwd=self.layout[name])
            self.assertEqual(got, "ssh -i /k -o IdentitiesOnly=yes")
        self.assertNotIn("core.sshCommand", subprocess.run(["git", "config", "--global", "--list"],
                                                           capture_output=True, text=True).stdout)
        self.assertEqual(self._provision(credentials).changes, [])

    def test_gh_sets_up_git_before_cloning(self):
        run = Recorder()
        with patch.object(pte, "missing_repositories", return_value=[]):
            _quiet(pte.provision_repositories, dict(self.layout, hams_shared=self.tmp + "/none"),
                   dict(self.credentials, method="gh"), run, list, pte.Report())
        self.assertEqual(run.calls[0], ["gh", "auth", "setup-git"])

    def test_missing_submodule_is_initialised(self):
        run = Recorder()
        with patch.object(pte, "missing_repositories", return_value=[]):
            _quiet(pte.provision_repositories, dict(self.layout, hams_shared=self.tmp + "/none"),
                   self.credentials, run, list, pte.Report())
        self.assertEqual(run.calls[-3][-4:], ["update", "--init", "--depth", "1"])
        self.assertEqual(run.calls[-2][-3:], ["1", "origin", "main"])
        self.assertEqual(run.calls[-1][-3:], ["checkout", "-q", "--detach", "FETCH_HEAD"][-3:])

    def test_plan_clones_nothing(self):
        out = io.StringIO()
        with redirect_stdout(out):
            pte.provision_repositories(self.layout, self.credentials, Recorder(), list, pte.Report(True), plan=True)
        self.assertFalse(os.path.exists(self.layout["hams_open"]))
        self.assertIn("PLAN run: git clone --depth 1", out.getvalue())


class RetryTests(unittest.TestCase):
    # Tests [@ANCHOR: provision_test_environment:retry]

    def test_retries_then_succeeds(self):
        attempts = []

        def action():
            attempts.append(1)
            if len(attempts) < 3:
                raise subprocess.CalledProcessError(1, "git")
            return "ok"

        sleeps = []
        self.assertEqual(_quiet(pte.retry, action, sleep=sleeps.append), "ok")
        self.assertEqual(sleeps, [30, 30])

    def test_gives_up_after_three(self):
        def action():
            raise subprocess.CalledProcessError(1, "git")

        with self.assertRaises(subprocess.CalledProcessError):
            _quiet(pte.retry, action, sleep=lambda s: None)


class AptSelectionTests(unittest.TestCase):
    # Tests [@ANCHOR: provision_test_environment:select_apt_packages]
    # Tests [@ANCHOR: infrastructure:test_environment_apt_packages]

    def test_manifest_test_profile_closes_the_known_gaps(self):
        names = {spec["name"] for spec in infrastructure.MANIFEST["test_environment_apt_packages"]}
        for needed in ("socat", "nodejs", "npm", "chromium", "golang-go", "iproute2", "python3-lxml-html-clean"):
            self.assertIn(needed, names)
        for spec in infrastructure.MANIFEST["test_environment_apt_packages"]:
            self.assertTrue(spec["why"], spec)
            self.assertLessEqual(set(spec), {"name", "debian_name", "why", "skip_if_command"})

    def test_test_profile_stays_out_of_what_test_py_requires_everywhere(self):
        # test.py's pre-flight checks "apt_packages" on every host; the test-only list must not be in it.
        prod = {spec["name"] for spec in infrastructure.MANIFEST["apt_packages"]}
        test_only = {spec["name"] for spec in infrastructure.MANIFEST["test_environment_apt_packages"]}
        self.assertEqual(prod & test_only, set())

    def test_selection(self):
        manifest = {
            "apt_packages": [
                {"name": "odoo", "environments": ["early_prod"]},
                {"name": "python3-pypdf2", "debian_name": "python3-pypdf", "environments": ["early_prod"]},
                {"name": "late", "environments": ["prod"]},
            ],
            "test_environment_apt_packages": [
                {"name": "socat", "why": "x"},
                {"name": "chromium", "why": "x", "skip_if_command": "google-chrome"},
            ],
        }
        wanted, skipped = pte.select_apt_packages(manifest, "debian", lambda c: c == "google-chrome")
        self.assertEqual(wanted, ["python3-pypdf", "socat"])
        self.assertEqual(skipped, {"chromium": "`google-chrome` is already on PATH"})
        wanted, skipped = pte.select_apt_packages(manifest, "ubuntu", lambda c: False)
        self.assertEqual(wanted, ["chromium", "python3-pypdf2", "socat"])
        self.assertEqual(skipped, {})


class AptProvisionTests(unittest.TestCase):
    # Tests [@ANCHOR: provision_test_environment:provision_apt_packages]

    def test_no_candidate_names_are_left_out_and_pip_fallbacks_reported(self):
        run = Recorder()
        seen = {}

        def fake_install(run_func, packages, opts, label):
            seen[label] = packages
            return []

        with patch.object(pte, "_apt_update_if_stale"), \
                patch.object(pte, "select_apt_packages", return_value=(["awscli", "nosuch", "socat"], {})), \
                patch.object(pte.infrastructure, "_installed_apt_packages", return_value=set()), \
                patch.object(pte, "_apt_candidate", side_effect=lambda p: p == "socat"), \
                patch.object(pte.infrastructure, "_apt_install_missing", side_effect=fake_install), \
                patch.object(pte.os.path, "isdir", return_value=False):
            report = pte.Report()
            no_candidate = _quiet(pte.provision_apt_packages, run, "ubuntu", report)
        self.assertEqual(no_candidate, ["awscli", "nosuch"])
        self.assertEqual(seen["test-environment"], ["socat"])
        self.assertEqual(report.skipped, ["apt: nosuch has no candidate on this release"])

    def test_an_apt_refusal_is_fatal(self):
        def refusing(run_func, packages, opts, label):
            infrastructure.record_hook_failure(f"apt_install:{label}", RuntimeError("would remove x"))
            return []

        with patch.object(pte.infrastructure, "_apt_install_missing", side_effect=refusing):
            with self.assertRaisesRegex(RuntimeError, "refused"):
                pte._install_apt(Recorder(), ["x"], "t", pte.Report())
        infrastructure.reset_hook_failures()


class PipTests(unittest.TestCase):
    # Tests [@ANCHOR: provision_test_environment:missing_pip_distributions]
    # Tests [@ANCHOR: provision_test_environment:provision_pip_packages]
    # Tests [@ANCHOR: infrastructure:runtime_pip_packages]
    # Tests [@ANCHOR: infrastructure:test_pip_packages]

    def test_missing(self):
        self.assertEqual(pte.missing_pip_distributions(["a", "b", "c"], lambda n: n == "b"), ["a", "c"])

    def test_nothing_installed_when_everything_is_present(self):
        run = Recorder()
        ok = subprocess.CompletedProcess([], 0)
        with patch.object(pte, "_pip_installed", return_value=lambda n: True), \
                patch.object(pte.subprocess, "run", return_value=ok):
            report = pte.Report()
            pte.provision_pip_packages(run, ["awscli"], report)
        self.assertEqual(run.calls, [])
        self.assertEqual(report.changes, [])

    def test_missing_groups_are_installed_as_provisioning_installs_them(self):
        run = Recorder()
        ok = subprocess.CompletedProcess([], 0)
        with patch.object(pte, "_pip_installed", return_value=lambda n: n not in ("hypothesis", "awscli")), \
                patch.object(pte.subprocess, "run", return_value=ok):
            _quiet(pte.provision_pip_packages, run, ["awscli"], pte.Report())
        self.assertEqual(run.calls[0][-1], "awscli")
        self.assertEqual(run.calls[1][-3:], list(infrastructure.TEST_PIP_PACKAGES) + ["--ignore-installed"])
        self.assertEqual(len(run.calls), 2)

    def test_the_lists_are_what_provision_environment_installs(self):
        self.assertIn("playwright", infrastructure.RUNTIME_PIP_PACKAGES)
        self.assertEqual(infrastructure.TEST_PIP_PACKAGES, ("hypothesis", "pip-audit"))


class CloudPythonTests(unittest.TestCase):
    # Tests [@ANCHOR: provision_test_environment:cloud_python_links]
    # Tests [@ANCHOR: provision_test_environment:distro_default_python]
    # Tests [@ANCHOR: provision_test_environment:provision_cloud_python]

    def test_links(self):
        self.assertEqual(pte.cloud_python_links("python3.12")[0], ("/usr/bin/python3", "python3.12"))
        self.assertIn(("/usr/local/bin/pip3", "/usr/bin/pip3"), pte.cloud_python_links("python3.12"))

    def test_distro_default(self):
        text = "[DEFAULT]\n# the default python3 version\ndefault-version = python3.13\nsupported-versions = x\n"
        self.assertEqual(pte.distro_default_python(text), "python3.13")
        with self.assertRaises(ValueError):
            pte.distro_default_python("[DEFAULT]\n")

    def test_the_real_defaults_file_parses_where_it_exists(self):
        if not os.path.exists(pte.DEBIAN_PYTHON_DEFAULTS):
            self.skipTest("not a Debian-family host")
        with open(pte.DEBIAN_PYTHON_DEFAULTS) as defaults:
            self.assertRegex(pte.distro_default_python(defaults.read()), r"^python3\.\d+$")

    def test_never_outside_a_cloud_session(self):
        run = Recorder()
        pte.provision_cloud_python({"CLAUDE_CODE_REMOTE": "false"}, run, pte.Report())
        pte.provision_cloud_python({}, run, pte.Report())
        self.assertEqual(run.calls, [])


class OdooTests(unittest.TestCase):
    # Tests [@ANCHOR: provision_test_environment:odoo_changelog_entry]
    # Tests [@ANCHOR: provision_test_environment:url_reachable]
    # Tests [@ANCHOR: provision_test_environment:provision_odoo]

    def test_changelog_version_sorts_below_a_nightly(self):
        entry = pte.odoo_changelog_entry("0123456789ab", "20261107", "Sat, 07 Nov 2026 12:00:00 +0000")
        self.assertTrue(entry.startswith("odoo (19.0.0~git20261107.0123456789ab-1) unstable; urgency=medium\n"))
        self.assertIn(" -- hams.com test environment <noreply@hams.com>  Sat, 07 Nov 2026", entry)
        if not shutil.which("dpkg"):
            self.skipTest("no dpkg")
        lower = subprocess.run(["dpkg", "--compare-versions", "19.0.0~git20261107.0123456789ab-1", "lt",
                                "19.0.20261107"])
        self.assertEqual(lower.returncode, 0)

    def test_url_reachable(self):
        def refused(request, timeout):
            self.assertEqual(request.get_method(), "HEAD")
            raise urllib.error.URLError("403")

        self.assertFalse(pte.url_reachable(pte.ODOO_KEY_URL, refused))
        response = MagicMock(status=200)
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        self.assertTrue(pte.url_reachable(pte.ODOO_KEY_URL, lambda request, timeout: response))

    def test_installed_odoo_is_left_alone(self):
        run = Recorder()
        reachable = MagicMock()
        with patch.object(pte.infrastructure, "_installed_apt_packages", return_value={"odoo"}):
            pte.provision_odoo(run, pte.Report(), systemd=True, reachable=reachable)
        self.assertEqual(run.calls, [])
        reachable.assert_not_called()

    def test_unreachable_nightly_builds_from_github(self):
        with patch.object(pte.infrastructure, "_installed_apt_packages", return_value=set()), \
                patch.object(pte, "_build_odoo_from_github") as build:
            run = Recorder()
            pte.provision_odoo(run, pte.Report(), systemd=False, reachable=lambda: False)
        build.assert_called_once()
        self.assertEqual(run.calls, [])

    def test_systemd_host_disables_the_odoo_service(self):
        with patch.object(pte.infrastructure, "_installed_apt_packages", return_value=set()), \
                patch.object(pte, "_build_odoo_from_github"):
            run = Recorder()
            pte.provision_odoo(run, pte.Report(), systemd=True, reachable=lambda: False)
        self.assertEqual(run.calls, [["systemctl", "disable", "--now", "odoo"]])


class KopiaAndUserToolTests(unittest.TestCase):
    # Tests [@ANCHOR: provision_test_environment:provision_kopia]
    # Tests [@ANCHOR: provision_test_environment:provision_user_tools]

    def test_kopia_on_path_is_left_alone(self):
        run = Recorder()
        with patch.object(pte.shutil, "which", return_value="/usr/bin/kopia"):
            pte.provision_kopia(run, pte.Report())
        self.assertEqual(run.calls, [])

    def test_user_tools_present_means_nothing_runs(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        os.makedirs(os.path.join(tmp, "shared", "node_modules", ".bin"))
        open(os.path.join(tmp, "shared", "node_modules", ".bin", "eslint"), "w").close()
        os.makedirs(os.path.join(tmp, "home", ".cache", "ms-playwright", "chromium_headless_shell-1"))
        run = Recorder()
        with patch.object(pte.shutil, "which", return_value="/usr/bin/cargo"), \
                patch.object(pte.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            report = pte.Report()
            pte.provision_user_tools({"hams_shared": os.path.join(tmp, "shared")}, run, list,
                                     os.path.join(tmp, "home"), report, reachable=lambda url: True)
        self.assertEqual(run.calls, [])
        self.assertEqual(report.changes, [])

    def test_missing_user_tools_run_as_the_user(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        run = Recorder()

        def as_user(argv):
            return ["AS-USER"] + list(argv)

        with patch.object(pte.shutil, "which", return_value="/usr/bin/cargo"), \
                patch.object(pte.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            _quiet(pte.provision_user_tools, {"hams_shared": tmp}, run, as_user, tmp, pte.Report(),
                   reachable=lambda url: True)
        self.assertEqual(run.calls[0], ["AS-USER", "cargo", "install", "--locked", "-q", "cargo-deny"])
        self.assertIn(["AS-USER", "env", "-u", "PLAYWRIGHT_BROWSERS_PATH", "python3", "-m", "playwright", "install",
                       "chromium-headless-shell"], run.calls)
        self.assertEqual(run.calls[-1], ["AS-USER", "npm", "ci", "--no-audit", "--no-fund", "-q"])

    def test_unreachable_playwright_cdn_is_a_reported_skip(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        os.makedirs(os.path.join(tmp, "node_modules", ".bin"))
        open(os.path.join(tmp, "node_modules", ".bin", "eslint"), "w").close()
        run = Recorder()
        with patch.object(pte.shutil, "which", return_value=None):
            report = pte.Report()
            _quiet(pte.provision_user_tools, {"hams_shared": tmp}, run, list, tmp, report,
                   reachable=lambda url: False)
        self.assertEqual(run.calls, [])
        self.assertTrue(any("cdn.playwright.dev" in text for text in report.skipped), report.skipped)


class ServiceAndDatabaseTests(unittest.TestCase):
    # Tests [@ANCHOR: provision_test_environment:offline_clusters]
    # Tests [@ANCHOR: provision_test_environment:provision_services]
    # Tests [@ANCHOR: provision_test_environment:role_sql]
    # Tests [@ANCHOR: provision_test_environment:provision_database]
    # Tests [@ANCHOR: provision_test_environment:wait_until]
    # Tests [@ANCHOR: provision_test_environment:host_alias_line]
    # Tests [@ANCHOR: provision_test_environment:missing_host_aliases]
    # Tests [@ANCHOR: provision_test_environment:provision_host_aliases]

    def test_offline_clusters(self):
        listing = ("16 main 5432 down postgres /var/lib/postgresql/16/main /var/log/x\n"
                   "18 main 5433 online postgres /var/lib/postgresql/18/main /var/log/y\n")
        self.assertEqual(pte.offline_clusters(listing), [("16", "main")])

    def test_without_systemd_the_offline_cluster_is_started_and_running_services_are_left_alone(self):
        run = Recorder()

        def probe(cmd, **kw):
            if cmd[0] == "pg_lsclusters":
                return subprocess.CompletedProcess(cmd, 0, stdout="16 main 5432 down postgres /d /l\n")
            if cmd[0] == "redis-cli":
                return subprocess.CompletedProcess(cmd, 0, stdout="PONG\n")
            return subprocess.CompletedProcess(cmd, 0, stdout="")

        with patch.object(pte.subprocess, "run", side_effect=probe):
            _quiet(pte.provision_services, run, pte.Report(), systemd=False)
        self.assertEqual(run.calls, [["pg_ctlcluster", "16", "main", "start"]])

    def test_without_systemd_redis_and_rabbitmq_are_started_as_their_own_users(self):
        run = Recorder()
        rabbit_probes = []

        def probe(cmd, **kw):
            if cmd[0] == "rabbitmqctl":
                rabbit_probes.append(cmd)
                return subprocess.CompletedProcess(cmd, 0 if len(rabbit_probes) > 1 else 69)
            return subprocess.CompletedProcess(cmd, 1, stdout="")

        with patch.object(pte.subprocess, "run", side_effect=probe), patch.object(pte.time, "sleep"):
            report = pte.Report()
            _quiet(pte.provision_services, run, report, systemd=False)
        self.assertEqual(run.calls[0][:4], ["runuser", "-u", "redis", "--"])
        self.assertEqual(run.calls[1], ["runuser", "-u", "rabbitmq", "--", "rabbitmq-server", "-detached"])
        self.assertEqual(report.changes, ["started redis-server (no systemd)", "started rabbitmq-server (no systemd)"])

    def test_wait_until(self):
        now = [0]
        answers = iter([False, False, True])
        self.assertTrue(pte.wait_until(lambda: next(answers), 10, sleep=lambda s: now.__setitem__(0, now[0] + s),
                                       clock=lambda: now[0]))
        self.assertFalse(pte.wait_until(lambda: False, 5, sleep=lambda s: now.__setitem__(0, now[0] + s),
                                        clock=lambda: now[0]))

    def test_host_aliases(self):
        line = pte.host_alias_line(infrastructure.MANIFEST)
        self.assertTrue(line.startswith("127.0.0.1 "))
        self.assertIn("redis", line.split())
        hosts = "127.0.0.1 localhost\n127.0.0.1 redis postgres # mine\n"
        self.assertEqual(pte.missing_host_aliases(hosts, "127.0.0.1 postgres redis rabbitmq"), ["rabbitmq"])
        self.assertEqual(pte.missing_host_aliases(hosts + "127.0.0.1 rabbitmq\n", "127.0.0.1 redis rabbitmq"), [])

    def test_host_aliases_are_appended_once_and_nothing_is_rewritten(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "hosts")
        with open(path, "w") as out:
            out.write("127.0.0.1 localhost\n10.0.0.5 vm")
        report = pte.Report()
        _quiet(pte.provision_host_aliases, report, path=path)
        with open(path) as hosts:
            text = hosts.read()
        self.assertTrue(text.startswith("127.0.0.1 localhost\n10.0.0.5 vm\n127.0.0.1 "))
        self.assertEqual(pte.missing_host_aliases(text, pte.host_alias_line(infrastructure.MANIFEST)), [])
        again = pte.Report()
        pte.provision_host_aliases(again, path=path)
        self.assertEqual(again.changes, [])

    def test_with_systemd_only_the_three_core_services_are_enabled(self):
        run = Recorder()
        with patch.object(pte.subprocess, "run", return_value=subprocess.CompletedProcess([], 3)):
            _quiet(pte.provision_services, run, pte.Report(), systemd=True)
        self.assertEqual([c[-1] for c in run.calls], ["postgresql", "redis-server", "rabbitmq-server"])
        for call in run.calls:
            self.assertEqual(call[:3], ["systemctl", "enable", "--now"])

    def test_role_sql(self):
        sql = pte.role_sql("ai")
        self.assertIn("CREATE ROLE odoo WITH SUPERUSER LOGIN PASSWORD 'odoo'", sql)
        self.assertIn('CREATE ROLE "ai" WITH SUPERUSER LOGIN', sql)
        self.assertNotIn('CREATE ROLE "postgres"', pte.role_sql("postgres"))
        for bad in ("a'b", "Robert); DROP", "", "x y"):
            with self.assertRaises(ValueError):
                pte.role_sql(bad)

    def test_database_steps_only_when_missing(self):
        def probe(cmd, **kw):
            text = "postgres\nodoo\nroot\n" if "pg_roles" in cmd[-1] else "postgres\nhams_test\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=text)

        run = Recorder()
        with patch.object(pte.subprocess, "run", side_effect=probe):
            pte.provision_database(run, "root", pte.Report())
        self.assertEqual(run.calls, [])

        def empty(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout="postgres\n")

        with patch.object(pte.subprocess, "run", side_effect=empty):
            _quiet(pte.provision_database, run, "ai", pte.Report())
        self.assertIn("CREATE ROLE", run.calls[0][-1])
        self.assertEqual(run.calls[1][-4:], ["createdb", "-O", "odoo", "hams_test"])


class ProfileTests(unittest.TestCase):
    # Tests [@ANCHOR: provision_test_environment:parse_profile]
    # Tests [@ANCHOR: provision_test_environment:profile_text]
    # Tests [@ANCHOR: provision_test_environment:profile_values]
    # Tests [@ANCHOR: provision_test_environment:provision_profile]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.layout = {"workspace": self.tmp}

    def test_round_trip(self):
        values = {"HAMS_CRYPTO_KEY": "ab+/=", "HAMS_BATCH_TREE_DIR": "/a b/c"}
        self.assertEqual(pte.parse_profile(pte.profile_text(values)), values)

    def test_new_key_is_32_random_bytes_and_an_existing_key_is_kept(self):
        first = pte.profile_values({}, {}, self.layout, 0o755)["HAMS_CRYPTO_KEY"]
        self.assertEqual(len(base64.b64decode(first)), 32)
        self.assertNotEqual(first, pte.profile_values({}, {}, self.layout, 0o755)["HAMS_CRYPTO_KEY"])
        self.assertEqual(pte.profile_values({"HAMS_CRYPTO_KEY": first}, {}, self.layout, 0o755)["HAMS_CRYPTO_KEY"],
                         first)

    def test_batch_dir_only_for_a_closed_home_and_lint_local_only_in_the_cloud(self):
        closed = pte.profile_values({}, {"CLAUDE_CODE_REMOTE": "true"}, self.layout, 0o700)
        self.assertEqual(closed["HAMS_BATCH_TREE_DIR"], os.path.join(self.tmp, "workspace", "tmp"))
        self.assertEqual(closed["HAMS_LINT_REMOTE"], "local")
        open_home = pte.profile_values({}, {}, self.layout, 0o755)
        self.assertEqual(set(open_home), {"HAMS_CRYPTO_KEY"})

    def test_written_once_key_never_printed_second_run_changes_nothing(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(home, mode=0o700)
        os.chmod(home, 0o700)
        path = os.path.join(self.tmp, "profile.sh")
        user = __import__("pwd").getpwuid(os.getuid()).pw_name
        out = io.StringIO()
        with redirect_stdout(out):
            report = pte.Report()
            values = pte.provision_profile(self.layout, {}, home, user, report, path=path)
        self.assertNotIn(values["HAMS_CRYPTO_KEY"], out.getvalue())
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
        self.assertEqual(len(report.changes), 3)  # profile, batch directory, .bashrc line
        self.assertTrue(os.path.isdir(values["HAMS_BATCH_TREE_DIR"]))
        again = pte.Report()
        self.assertEqual(pte.provision_profile(self.layout, {}, home, user, again, path=path), values)
        self.assertEqual(again.changes, [])
        with open(os.path.join(home, ".bashrc")) as bashrc:
            self.assertEqual(bashrc.read().count(path), 2)  # one line: `[ -f P ] && . P`


class SelfCheckTests(unittest.TestCase):
    # Tests [@ANCHOR: provision_test_environment:self_check_commands]
    # Tests [@ANCHOR: provision_test_environment:run_self_check]

    layout = {"hams_com": "/w/hams_com"}

    def test_commands(self):
        commands, skipped = pte.self_check_commands(self.layout, True)
        self.assertEqual([c[1] for c in commands], [
            ["python3", "tools/run_linters.py", "--changed"],
            ["python3", "hams_shared/tools/test.py", "-u", "zero_sudo", "-d", "hams_test"],
        ])
        self.assertEqual(skipped, [])

    def test_dirty_tree_skips_the_linters(self):
        commands, skipped = pte.self_check_commands(self.layout, False)
        self.assertEqual(len(commands), 1)
        self.assertIn("uncommitted", skipped[0])

    def test_failures_are_returned(self):
        def runner(cmd, **kw):
            if cmd[:2] == ["git", "-C"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="")
            return subprocess.CompletedProcess(cmd, 1 if "test.py" in cmd[1] else 0)

        failed = _quiet(pte.run_self_check, self.layout, {}, list, pte.Report(), runner)
        self.assertEqual(failed, ["test.py -u zero_sudo"])


class SmallPieceTests(unittest.TestCase):
    # Tests [@ANCHOR: provision_test_environment:as_user_command]
    # Tests [@ANCHOR: provision_test_environment:report]

    def test_as_user(self):
        with patch.object(pte.os, "geteuid", return_value=0):
            self.assertEqual(pte.as_user_command("ai", "/srv/users/ai")(["npm", "ci"]),
                             ["runuser", "-u", "ai", "--", "env", "HOME=/srv/users/ai", "npm", "ci"])
            self.assertEqual(pte.as_user_command("root", "/root")(["npm"]), ["npm"])
        with patch.object(pte.os, "geteuid", return_value=1000):
            self.assertEqual(pte.as_user_command("ai", "/srv/users/ai")(["npm"]), ["npm"])

    def test_report(self):
        out = io.StringIO()
        with redirect_stdout(out):
            report = pte.Report(plan=True)
            report.changed("x")
            report.skip("y")
            report.note("z")
        self.assertEqual((report.changes, report.skipped, report.notes), (["x"], ["y"], ["z"]))
        self.assertIn("WOULD CHANGE: x", out.getvalue())


class LockTests(unittest.TestCase):
    # Tests [@ANCHOR: provision_test_environment:acquire_lock]

    def test_a_second_run_cannot_take_the_lock(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "lock")
        first = pte.acquire_lock(path)
        self.addCleanup(first.close)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with self.assertRaisesRegex(RuntimeError, "holds"):
            pte.acquire_lock(path, wait=False)
        first.close()
        pte.acquire_lock(path, wait=False).close()


class StartFinishTests(unittest.TestCase):
    # Tests [@ANCHOR: provision_test_environment:start]
    # Tests [@ANCHOR: provision_test_environment:finish]

    def test_start_refuses_production_before_any_repository_work(self):
        with patch.object(pte, "list_database_names", return_value=("hams_prod",)), \
                patch.object(pte, "acquire_lock") as lock, patch.object(pte, "provision_repositories") as repos:
            with self.assertRaises(SystemExit):
                _quiet(pte.start, Recorder(), {}, "root", "/t", None, workspace="/w")
        repos.assert_not_called()
        lock.assert_not_called()

    def test_finish_order_and_self_check_failure_exits_1(self):
        order = []
        names = ["provision_cloud_python", "provision_apt_packages", "provision_pip_packages", "provision_odoo",
                 "provision_kopia", "provision_host_aliases", "provision_services", "provision_database",
                 "provision_user_tools", "provision_profile"]
        patches = [patch.object(pte, n, side_effect=lambda *a, _n=n, **k: order.append(_n) or ([] if _n.endswith(
            "apt_packages") else {})) for n in names]
        patches.append(patch.object(pte, "run_self_check", return_value=["test.py -u zero_sudo"]))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        state = {"report": pte.Report(), "layout": {}, "as_user": list, "runner_user": "root", "home": "/root",
                 "systemd": False}
        with self.assertRaises(SystemExit) as caught:
            _quiet(pte.finish, state, Recorder(), {}, "ubuntu")
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(order, names)

    def test_finish_plan_runs_no_self_check_and_no_database(self):
        names = ["provision_cloud_python", "provision_apt_packages", "provision_pip_packages", "provision_odoo",
                 "provision_kopia", "provision_host_aliases", "provision_services", "provision_user_tools",
                 "provision_profile"]
        for n in names:
            p = patch.object(pte, n, return_value=[] if n.endswith("apt_packages") else {})
            p.start()
            self.addCleanup(p.stop)
        with patch.object(pte, "run_self_check") as check, patch.object(pte, "provision_database") as database:
            state = {"report": pte.Report(True), "layout": {}, "as_user": list, "runner_user": "root",
                     "home": "/root", "systemd": False}
            _quiet(pte.finish, state, Recorder(), {}, "ubuntu", plan=True)
        check.assert_not_called()
        database.assert_not_called()


class PrivateHelperTests(unittest.TestCase):
    # Tests [@ANCHOR: provision_test_environment:_apt_candidate]
    # Tests [@ANCHOR: provision_test_environment:_apt_update_if_stale]
    # Tests [@ANCHOR: provision_test_environment:_install_apt]
    # Tests [@ANCHOR: provision_test_environment:_pip_installed]
    # Tests [@ANCHOR: provision_test_environment:_psql]
    # Tests [@ANCHOR: provision_test_environment:_set_git_config]
    # Tests [@ANCHOR: provision_test_environment:_build_odoo_from_github]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_apt_candidate_reads_apt_cache_policy(self):
        policy = "socat:\n  Installed: (none)\n  Candidate: 1.8.0.0-4build3\n"
        with patch.object(pte.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=policy)):
            self.assertTrue(pte._apt_candidate("socat"))
        none = "awscli:\n  Installed: (none)\n  Candidate: (none)\n"
        with patch.object(pte.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=none)):
            self.assertFalse(pte._apt_candidate("awscli"))
        with patch.object(pte.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="")):
            self.assertFalse(pte._apt_candidate("unknown"))

    def test_apt_update_only_when_the_stamp_is_old(self):
        stamp = os.path.join(self.tmp, "stamp")
        with patch.object(pte, "APT_UPDATE_STAMP", stamp):
            run = Recorder()
            _quiet(pte._apt_update_if_stale, run, pte.Report(), False)
            self.assertEqual(run.calls, [["apt-get", "update", "-q"]])
            self.assertTrue(os.path.exists(stamp))
            _quiet(pte._apt_update_if_stale, run, pte.Report(), False)
            self.assertEqual(len(run.calls), 1)
            _quiet(pte._apt_update_if_stale, run, pte.Report(), False, now=os.path.getmtime(stamp) + 13 * 3600)
            self.assertEqual(len(run.calls), 2)

    def test_install_apt_reports_what_was_installed(self):
        with patch.object(pte.infrastructure, "_apt_install_missing", return_value=["socat"]):
            report = pte.Report()
            _quiet(pte._install_apt, Recorder(), ["socat"], "t", report)
        self.assertEqual(report.changes, ["apt (t) installed socat"])

    def test_pip_installed_probes_the_named_interpreter(self):
        probe = pte._pip_installed(sys.executable)
        present = next(iter(importlib.metadata.distributions())).metadata["Name"]
        self.assertTrue(probe(present))
        self.assertFalse(probe("no-such-distribution-hams-test"))

    def test_psql_argv(self):
        argv = pte._psql("SELECT 1")
        self.assertEqual(argv[:4], ["runuser", "-u", "postgres", "--"])
        self.assertIn("ON_ERROR_STOP=1", argv)
        self.assertEqual(argv[-1], "SELECT 1")

    def test_set_git_config_on_a_missing_checkout_does_nothing(self):
        run = Recorder()
        pte._set_git_config(os.path.join(self.tmp, "absent"), "core.sshCommand", "x", run, list, pte.Report(), False)
        self.assertEqual(run.calls, [])

    def test_build_installs_an_already_built_package_without_rebuilding(self):
        os.makedirs(os.path.join(self.tmp, "src", ".git"))
        deb = os.path.join(self.tmp, "odoo_19.0.0~git20261107.0123456789ab-1_all.deb")
        open(deb, "w").close()
        run = Recorder()
        with patch.object(pte, "ODOO_DIR", self.tmp), patch.object(pte, "_install_apt"):
            report = pte.Report()
            _quiet(pte._build_odoo_from_github, run, report, False)
        self.assertEqual(run.calls, [["apt-get", "install", "-y", "-q", "--no-install-recommends", deb]])
        self.assertEqual(report.changes, [f"installed {deb}"])


if __name__ == "__main__":
    unittest.main()
