#!/usr/bin/env python3
# This software is distributed under the terms of the GNU General Public License, version 3.
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Integration test for odoo_mypy_plugin.py (ODOO_AWARE_TYPE_CHECKING.md Phase 2, step 3).

Shells out to real mypy against a real fixture, because the thing being tested -- whether
get_customize_class_mro_hook actually resolves a method across two textually unrelated
_inherit contributors -- is a property of mypy's own build graph and semantic analysis, not
something a pure-Python unit test of this plugin's helper functions could observe. Slower than
the rest of this tool family's test suite (builds the real hams_com/hams_open registry once per
run); run it deliberately, not as part of a tight edit-test loop.

The fixture is written into a tempfile.mkdtemp() directory for the duration of each test and
removed in tearDown, and that directory is handed to the plugin (a mypy subprocess) as an extra
addon root via ODOO_MYPY_PLUGIN_EXTRA_ROOTS, so odoo_mypy_plugin.py's own path-based fullname
resolution still finds it. It used to be written into hams_com/_test_scratch_mypy_plugin/, but
that put a half-built, unimported addon inside the repo root where any concurrent repo-scanning
checker saw it -- notably check_init_imports.py in test.py's pre-flight, which aborted unrelated
Odoo test runs by other sessions with "File 'relational_probe.py' ... is never imported" whenever
run_linters.py's step 27 (this suite) was running at the same time.

These tests were intermittently flaky (mypy producing empty/wrong output, or a plain "No such
file or directory" for a hams_com-only path) specifically when run via run_linters.py, never when
run standalone. Two wrong theories were tried and disproven before the real cause was found by
direct instrumentation, not assumed:
  1. Concurrent resource contention with a heavy Odoo test suite running in the background. The
     correlation was real (re-running after that suite finished coincidentally "worked"), but no
     code change was made on this theory and it was never actually verified.
  2. A shared `.mypy_cache` collision with check_untyped_utility_files.py's own plain-mypy
     invocation (Phase 1, no plugin, no odoo_type_stubs) against the same repo root, which
     precedes this suite's step in run_linters.py and, like this suite at the time, used no
     explicit `--cache-dir`. Isolating this suite's cache dir (see setUp/tearDown) is a real,
     independently-justified fix -- two different mypy configs should never share one incremental
     cache -- but it did NOT resolve the flakiness. That disproved the theory.
The actual root cause: `_find_hams_com()` (below) assumed the repo containing this file was always
hams_open and always searched for a "hams_com" sibling. hams_com/hams_shared is a symlink to
hams_open/hams_shared, so `__file__` for this same file resolves through WHICHEVER path it was
actually reached by -- a plain shell invocation from hams_com's own root resolves it as living
inside hams_com, but run_linters.py's subprocess spawning resolved it through the symlink into
hams_open instead. When that happened, the old logic found hams_open "as its own sibling" and
silently ran mypy against the wrong repo, producing exactly this suite's symptoms for any
hams_com-only fixture path (e.g. ham_dns/). Fixed by making `_find_hams_com()`/`_OWN_REPO_DIR`
genuinely direction-agnostic instead of assuming one particular parent (see the comment there).

**A second, worse instance of the same underlying problem, found later the same night, auditing
run_linters.py's own `dir_path` bug (see docs/proposals/LINTER_POLICY_REVISIT.md).** `_TOOLS_DIR`/
`_HAMS_SHARED_DIR`/`_OWN_REPO_DIR` used a fixed-depth `os.path.dirname()` chain, assuming
`__file__`'s literal (non-symlink-resolved) path always had the shape `<repo>/hams_shared/tools/
test_odoo_mypy_plugin.py`. `hams_open/tools` and `hams_com/tools` are THEMSELVES symlinks straight
to `hams_shared/tools` (one hop, not two) -- when this test suite is invoked with a file argument
like `<workspace>/hams_open/tools/test_odoo_mypy_plugin.py` (exactly what run_linters.py's own step
27 passes when correctly invoked from a real repo root), the literal path is one directory level
SHALLOWER than the `hams_shared/tools/...` shape the fixed dirname chain assumed, so
`_OWN_REPO_DIR` silently resolved to the parent workspace directory itself, not any repo.
`_find_hams_com()` then searched for a `hams_com`/`hams_open` sibling of that workspace directory,
and by sheer coincidence a real, unrelated, non-git directory happening to be named `hams_com`
existed elsewhere on this machine outside the normal workspace (confirmed directly: `git remote -v`
inside it fails with "not a git repository") -- so instead of failing loudly, `_HAMS_COM_DIR`
silently resolved to that wrong directory, and this suite briefly wrote its gitignored scratch
fixture there before its own tearDown cleaned it up. No lasting artifact was left, but this is a
strictly worse failure mode than the first bug: a wrong-but-real directory outside this repo
entirely, not just a wrong sibling within it. Fixed by resolving `_TOOLS_DIR` through
`os.path.realpath()` (which follows symlinks)
before applying the dirname chain, rather than `os.path.abspath()` (which does not) -- this makes
the computation correct regardless of how many symlink hops were used to reach this file, instead
of assuming a specific path depth.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_TOOLS_DIR = os.path.dirname(os.path.realpath(__file__))
# realpath(), not abspath(): abspath() makes a path absolute but does NOT resolve symlinks, and
# both hams_open/tools and hams_com/tools are themselves symlinks straight to hams_shared/tools
# (one hop, not two, via the intermediate hams_shared/ directory) -- a fixed-depth dirname chain
# starting from the literal, symlink-preserved path silently resolves one directory level too
# shallow when this file is reached through that direct tools/ symlink (exactly what happens when
# run_linters.py's own subprocess passes a file argument like ".../hams_open/tools/
# test_odoo_mypy_plugin.py"), landing on the *workspace* directory instead of any repo. realpath()
# collapses every symlink hop first, so the dirname chain below always starts from the one real,
# canonical path (".../hams_open/hams_shared/tools/...") regardless of which symlink was used to
# reach this file.
_HAMS_SHARED_DIR = os.path.dirname(_TOOLS_DIR)
# hams_com/hams_shared is a symlink to hams_open/hams_shared -- even after realpath(), "the repo
# containing me" -- call it _OWN_REPO_DIR -- might genuinely be hams_com OR hams_open depending on
# which repo's hams_shared this file was reached through (confirmed directly: differs between a
# standalone pytest run and a run_linters.py-invoked one). It must not be assumed to always be
# hams_open the way an earlier version of this file did (that bug: it always called the parent
# "_HAMS_OPEN_DIR" and only ever looked for a "hams_com" sibling, so when _OWN_REPO_DIR really was
# hams_com, _find_sibling_repo correctly found hams_open as ITS sibling and this file silently used
# hams_open where it needed hams_com, producing "No such file or directory" for hams_com-only paths
# like ham_dns/).
_OWN_REPO_DIR = os.path.dirname(_HAMS_SHARED_DIR)

# Both _find_hams_com() and _find_hams_open() need odoo_registry_builder's
# _find_sibling_repo(), and both used to re-do this same sys.path.insert() +
# import locally on every call -- but _HAMS_COM_DIR/_HAMS_OPEN_DIR below call
# them exactly once each, unconditionally, at module-import time, so there
# was never any actual laziness to gain: the import ran immediately either
# way. Hoisted to module scope (satisfies check_burn_list.py's LOCAL IMPORT
# rule) with the exact same sys.path.insert(0, _TOOLS_DIR) defensiveness
# this file's own docstring justifies above -- this test file can be reached
# through more than one symlink path depending on invocation (pytest vs.
# run_linters.py's subprocess), so it doesn't rely on Python's own automatic
# sys.path[0] insertion being the real tools/ directory.
sys.path.insert(0, _TOOLS_DIR)
import odoo_registry_builder as orb  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _find_hams_com():
    if os.path.basename(_OWN_REPO_DIR) == "hams_com":
        return _OWN_REPO_DIR
    sibling = orb._find_sibling_repo(_OWN_REPO_DIR)
    if sibling and os.path.basename(sibling) == "hams_com":
        return sibling
    return None


def _find_hams_open():
    # Mirrors _find_hams_com() exactly, the other direction -- same
    # discipline this file's own module docstring insists on: never assume
    # a fixed directory relationship (this repo pair's mutual-symlink
    # structure has already produced two real, confirmed bugs from doing
    # that), always resolve through the same real, robust sibling lookup.
    if os.path.basename(_OWN_REPO_DIR) == "hams_open":
        return _OWN_REPO_DIR
    sibling = orb._find_sibling_repo(_OWN_REPO_DIR)
    if sibling and os.path.basename(sibling) == "hams_open":
        return sibling
    return None


_HAMS_COM_DIR = _find_hams_com()
_HAMS_OPEN_DIR = _find_hams_open()
_SCRATCH_PKG_NAME = "_test_scratch_mypy_plugin"
_SIBLINGS_PKG_NAME = "_test_scratch_mypy_plugin_class_siblings"


@unittest.skipUnless(_HAMS_COM_DIR, "hams_com sibling repo not found -- plugin needs both repos")
class OdooMypyPluginRealCodeTests(unittest.TestCase):
    """Validates the MRO-injection mechanism against real production code, not a synthetic model."""

    def setUp(self):
        # Isolated cache dir, not mypy's default .mypy_cache under
        # _HAMS_COM_DIR -- run_linters.py's own check_untyped_utility_files.py
        # step (Phase 1) also invokes plain mypy against this same repo
        # root with a DIFFERENT config (no plugin, no odoo_type_stubs) and
        # no --cache-dir of its own, so it would otherwise write to that
        # same default cache. Two different mypy configs should never share
        # one incremental cache regardless of whether that collision was
        # ever this suite's actual flakiness -- it wasn't (see the module
        # docstring for the real root cause, a _find_hams_com() bug); this
        # isolation is kept as an independently-justified improvement.
        self._cache_dir = tempfile.mkdtemp(prefix="odoo_mypy_plugin_test_cache_")
        # Fixture root outside any repo (see module docstring); realpath so it matches the plugin's own
        # realpath() of the same directory.
        self._scratch_root = os.path.realpath(tempfile.mkdtemp(prefix="odoo_mypy_plugin_test_scratch_"))
        self._scratch_dir = os.path.join(self._scratch_root, _SCRATCH_PKG_NAME)
        self._ini_path = os.path.join(self._scratch_root, "mypy.ini")
        _write(
            self._ini_path,
            "[mypy]\n"
            "ignore_missing_imports = True\n"
            "check_untyped_defs = True\n"
            "follow_imports = silent\n"
            f"mypy_path = {os.path.join(_HAMS_COM_DIR, 'hams_shared', 'tools', 'odoo_type_stubs')}\n"
            f"plugins = {os.path.join(_HAMS_COM_DIR, 'hams_shared', 'tools', 'odoo_mypy_plugin.py')}\n",
        )
        _write(os.path.join(self._scratch_dir, "__init__.py"), "from . import models\n")
        _write(os.path.join(self._scratch_dir, "__manifest__.py"), "{'name': 'mypy plugin test scratch', 'description': 'Scratch fixture for test_odoo_mypy_plugin.py, not a real module.', 'depends': []}\n")
        _write(os.path.join(self._scratch_dir, "models", "__init__.py"), "from . import res_users_probe\n")
        _write(
            os.path.join(self._scratch_dir, "models", "res_users_probe.py"),
            "from odoo import models\n"
            "\n"
            "\n"
            "class ResUsersProbe(models.Model):\n"
            "    _inherit = 'res.users'\n"
            "\n"
            "    def probe_real_cross_file_method(self) -> None:\n"
            "        # _provision_personal_dns_zone is real, declared only in\n"
            "        # ham_dns/models/res_users.py -- a different, textually\n"
            "        # unrelated file/class. Must resolve if MRO injection works.\n"
            "        self._provision_personal_dns_zone()\n"
            "\n"
            "    def probe_nonexistent_method(self) -> None:\n"
            "        self._this_method_genuinely_does_not_exist_anywhere()\n",
        )
        # Phase 2 step 2 (comodel resolution): a Many2one targeting the real, multi-contributor
        # res.users model. probe_real_cross_module_via_relational only resolves if the returned
        # type is the FULL merged res.users, not just whatever a single file declares.
        _write(
            os.path.join(self._scratch_dir, "models", "relational_probe.py"),
            "from odoo import models, fields\n"
            "\n"
            "\n"
            "class RelationalProbe(models.Model):\n"
            "    _name = 'scratch.relational.probe'\n"
            "\n"
            "    user_id = fields.Many2one('res.users', string='User')\n"
            "\n"
            "    def probe_real_cross_module_via_relational(self) -> None:\n"
            "        self.user_id._provision_personal_dns_zone()\n"
            "\n"
            "    def probe_nonexistent_via_relational(self) -> None:\n"
            "        self.user_id._this_method_genuinely_does_not_exist_anywhere()\n",
        )
        # Phase 2 step 4 (env['some.model'] resolution): same real cross-module method, reached
        # via self.env['res.users'] instead of a Many2one field.
        _write(
            os.path.join(self._scratch_dir, "models", "env_probe.py"),
            "from odoo import models\n"
            "\n"
            "\n"
            "class EnvProbe(models.Model):\n"
            "    _name = 'scratch.env.probe'\n"
            "\n"
            "    def probe_real_cross_module_via_env(self) -> None:\n"
            "        self.env['res.users']._provision_personal_dns_zone()\n"
            "\n"
            "    def probe_nonexistent_via_env(self) -> None:\n"
            "        self.env['res.users']._this_method_genuinely_does_not_exist_anywhere()\n",
        )
        # Mixin injection (ODOO_AWARE_TYPE_CHECKING.md's own "smaller gap found but NOT fixed"
        # note, since closed): the same shape as the real production bug
        # (user_websites_seo/models/res_users.py's real ResUsersSEO -- _name = "res.users",
        # _inherit = ["res.users", "user.websites.seo.metadata.mixin"] -- calling
        # _get_seo_fields(), defined only on the mixin, a genuinely different model reached
        # through Odoo's own runtime registry merging, never a real Python base class). This
        # probe reuses the SAME real mixin model already in the registry (no new fixture
        # module needed for the mixin side) rather than inventing a synthetic one, so this
        # test exercises the real _get_seo_fields resolution end to end, not just a shape
        # that resembles it.
        _write(
            os.path.join(self._scratch_dir, "models", "mixin_probe.py"),
            "from odoo import models\n"
            "\n"
            "\n"
            "class MixinProbe(models.Model):\n"
            "    _name = 'res.users'\n"
            "    _inherit = ['res.users', 'user.websites.seo.metadata.mixin']\n"
            "\n"
            "    def probe_real_mixin_method(self) -> None:\n"
            "        # _get_seo_fields is real, declared only on\n"
            "        # user_websites_seo/models/seo_metadata_mixin.py's mixin class --\n"
            "        # never a same-model res.users sibling, only reachable via this\n"
            "        # contributor's own second, non-primary _inherit target. Must\n"
            "        # resolve if mixin injection works.\n"
            "        self._get_seo_fields()\n"
            "\n"
            "    def probe_nonexistent_mixin_method(self) -> None:\n"
            "        self._this_mixin_method_genuinely_does_not_exist_anywhere()\n",
        )

    def tearDown(self):
        shutil.rmtree(self._scratch_root, ignore_errors=True)
        shutil.rmtree(self._cache_dir, ignore_errors=True)

    def _run_mypy(self, *files):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mypy",
                *files,
                "--config-file",
                self._ini_path,
                "--cache-dir",
                self._cache_dir,
            ],
            cwd=_HAMS_COM_DIR,
            env={**os.environ, "ODOO_MYPY_PLUGIN_EXTRA_ROOTS": self._scratch_root},
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.stdout

    def test_real_cross_file_inherit_method_resolves_when_sibling_is_explicitly_passed(self):
        probe = os.path.join(self._scratch_dir, "models", "res_users_probe.py")
        sibling = os.path.join(_HAMS_COM_DIR, "ham_dns", "models", "res_users.py")
        output = self._run_mypy(probe, sibling)
        self.assertNotIn("_provision_personal_dns_zone", output, output)

    def test_a_genuinely_nonexistent_method_is_still_flagged(self):
        probe = os.path.join(self._scratch_dir, "models", "res_users_probe.py")
        sibling = os.path.join(_HAMS_COM_DIR, "ham_dns", "models", "res_users.py")
        output = self._run_mypy(probe, sibling)
        self.assertIn("_this_method_genuinely_does_not_exist_anywhere", output, output)

    def test_get_additional_deps_pulls_in_the_sibling_even_when_not_explicitly_passed(self):
        # The discriminating case for get_additional_deps: pass ONLY the
        # probe file, not its sibling. Without get_additional_deps forcing
        # mypy's build graph to analyze ham_dns/models/res_users.py too,
        # lookup_fully_qualified_or_none silently returns None for the
        # un-analyzed sibling and this method wrongly reports as missing --
        # confirmed empirically earlier the same session this test was
        # written (single-file case predates the fix and fails without it).
        probe = os.path.join(self._scratch_dir, "models", "res_users_probe.py")
        output = self._run_mypy(probe)
        self.assertNotIn("_provision_personal_dns_zone", output, output)

    # --- Phase 2 step 2: fields.Many2one/One2many/Many2many comodel resolution ---

    def test_many2one_comodel_resolves_to_the_full_merged_model_without_the_sibling_passed(self):
        # The discriminating case for the get_additional_deps extension this step needed: pass
        # ONLY the probe file. A real Many2one comodel reference is a string literal, not an
        # import, so ham_dns/models/res_users.py is never in the build unless
        # _file_comodel_targets/get_additional_deps forces it in -- confirmed empirically this
        # session that without that extension, named_generic_type/lookup_qualified fails even
        # when the target module genuinely is in the build (see odoo_mypy_plugin.py's own
        # _resolve_model_instance docstring for the deeper reason: the officially-declared
        # named_generic_type path assumes an import chain that Odoo's string-based model
        # references never create).
        probe = os.path.join(self._scratch_dir, "models", "relational_probe.py")
        output = self._run_mypy(probe)
        self.assertNotIn("_provision_personal_dns_zone", output, output)

    def test_a_genuinely_nonexistent_method_via_a_relational_field_is_still_flagged(self):
        probe = os.path.join(self._scratch_dir, "models", "relational_probe.py")
        output = self._run_mypy(probe)
        self.assertIn("_this_method_genuinely_does_not_exist_anywhere", output, output)

    # --- Phase 2 step 4: env['some.model'] resolution ---

    def test_env_getitem_resolves_to_the_full_merged_model_without_the_sibling_passed(self):
        probe = os.path.join(self._scratch_dir, "models", "env_probe.py")
        output = self._run_mypy(probe)
        self.assertNotIn("_provision_personal_dns_zone", output, output)

    def test_a_genuinely_nonexistent_method_via_env_getitem_is_still_flagged(self):
        probe = os.path.join(self._scratch_dir, "models", "env_probe.py")
        output = self._run_mypy(probe)
        self.assertIn("_this_method_genuinely_does_not_exist_anywhere", output, output)

    # --- Mixin injection (a second, different model named in the same contributor's own
    # _inherit list, not a same-model sibling). Sibling passed explicitly here, matching
    # test_real_cross_file_inherit_method_resolves_when_sibling_is_explicitly_passed's own
    # pattern above -- this tests the MRO-injection mechanism itself in isolation from
    # get_additional_deps's own single-file pulling-in behavior (already covered generally by
    # test_get_additional_deps_pulls_in_the_sibling_even_when_not_explicitly_passed above; not
    # re-proven per sibling-relationship kind here). The real, unmocked mechanism was also
    # independently confirmed against real production code the same session this was written:
    # a live 26-file mypy sweep including the actual user_websites_seo/models/res_users.py
    # went from 13 to 11 errors with this fix, with the real ResUsersSEO._get_seo_fields() call
    # (and a second, structurally identical real bug, ics_forms's message_new via mail.thread)
    # both newly resolving -- see ODOO_AWARE_TYPE_CHECKING.md's own dated update.

    @unittest.skipUnless(_HAMS_OPEN_DIR, "hams_open sibling repo not found -- the real mixin lives there")
    def test_a_mixin_only_method_resolves_via_the_contributors_own_extra_inherit_target(self):
        probe = os.path.join(self._scratch_dir, "models", "mixin_probe.py")
        mixin_sibling = os.path.join(_HAMS_OPEN_DIR, "user_websites_seo", "models", "seo_metadata_mixin.py")
        output = self._run_mypy(probe, mixin_sibling)
        self.assertNotIn("_get_seo_fields", output, output)

    @unittest.skipUnless(_HAMS_OPEN_DIR, "hams_open sibling repo not found -- the real mixin lives there")
    def test_a_genuinely_nonexistent_mixin_method_is_still_flagged(self):
        probe = os.path.join(self._scratch_dir, "models", "mixin_probe.py")
        mixin_sibling = os.path.join(_HAMS_OPEN_DIR, "user_websites_seo", "models", "seo_metadata_mixin.py")
        output = self._run_mypy(probe, mixin_sibling)
        self.assertIn("_this_mixin_method_genuinely_does_not_exist_anywhere", output, output)

    # --- Transitive (2+ hop) mixin chain: a REAL bug found auditing this file, not synthetic.
    # `ham.club.owned.mixin` (hams_com/ham_club_management/models/ham_club_features.py's
    # HamClubOwnedMixin) declares zero methods of its own -- it depends entirely on a SECOND
    # mixin, `_inherit = ["ham.club.owned.mixin", "user_websites.owned.mixin"]`, for
    # `_check_proxy_ownership_create`/`_check_proxy_ownership_write`. Real, currently-shipping
    # code two hops away (ham_club_management/models/survey_extensions.py's SurveyUserInput,
    # `_inherit = ["survey.user_input", "ham.club.owned.mixin"]`) genuinely calls both methods
    # at lines 140/170. Confirmed directly, before this fix existed: running the real plugin
    # config against survey_extensions.py (with user_websites_owned_mixin.py passed explicitly,
    # the same "sibling passed explicitly" pattern the mixin tests above already use, for the
    # same already-documented "get_additional_deps' own single-file pulling-in behavior for
    # mixin injection" reason cited above) reported both real calls as
    # `"SurveyUserInput" has no attribute "_check_proxy_ownership_create"`/
    # `"_check_proxy_ownership_write"` -- a live false positive on real production code. Root
    # cause: `_compute_sibling_map`'s mixin injection only walked ONE hop (`ham.club.owned.mixin`'s
    # own further `_inherit` target was read into a variable deliberately named with a leading
    # underscore, then discarded). Fixed by `_transitive_mixin_contributors`, a BFS that walks
    # every hop, with a visited-model-names cycle guard.
    #
    # These tests use the real production files directly (no synthetic scratch fixture) --
    # this bug is about real, already-shipping code, not a shape worth reproducing synthetically.
    @unittest.skipUnless(_HAMS_OPEN_DIR, "hams_open sibling repo not found -- the second-hop mixin lives there")
    def test_transitive_mixin_chain_two_hops_away_resolves(self):
        real_file = os.path.join(_HAMS_COM_DIR, "ham_club_management", "models", "survey_extensions.py")
        second_hop_mixin = os.path.join(_HAMS_OPEN_DIR, "user_websites", "models", "user_websites_owned_mixin.py")
        output = self._run_mypy(real_file, second_hop_mixin)
        self.assertNotIn("_check_proxy_ownership_create", output, output)
        self.assertNotIn("_check_proxy_ownership_write", output, output)


@unittest.skipUnless(_HAMS_COM_DIR, "hams_com sibling repo not found -- plugin needs both repos")
class OdooMypyPluginClassSiblingsRegressionTests(unittest.TestCase):
    """Regression coverage for the real bug found and fixed this session in
    OdooPlugin._compute_sibling_map: `_class_siblings[fn] = [...]` (assignment) silently
    OVERWROTE, rather than accumulated, a contributor class's sibling list whenever that same
    class fullname contributed to more than one model -- exactly the mixin self-reference idiom
    (`_inherit = ["model.a", "model.b"]`, no `_name`) already covered elsewhere in this file as
    correctly registry-merged. The registry side was always correct; this class regression-tests
    the plugin's own MRO-injection side, which silently lost one of the two sibling lists
    whenever the OTHER model happened to be processed later in registry-iteration order --
    confirmed as a real bug via a minimal reproduction against real production code
    (user_websites/models/res_users.py's own dual-target _inherit) before being fixed; see
    ODOO_AWARE_TYPE_CHECKING.md's dated section for this session for that real-code trace."""

    def setUp(self):
        self._cache_dir = tempfile.mkdtemp(prefix="odoo_mypy_plugin_siblings_test_cache_")
        self._scratch_root = os.path.realpath(tempfile.mkdtemp(prefix="odoo_mypy_plugin_siblings_test_scratch_"))
        self._scratch_dir = os.path.join(self._scratch_root, _SIBLINGS_PKG_NAME)
        self._ini_path = os.path.join(self._scratch_root, "mypy.ini")
        _write(
            self._ini_path,
            "[mypy]\n"
            "ignore_missing_imports = True\n"
            "check_untyped_defs = True\n"
            "follow_imports = silent\n"
            f"mypy_path = {os.path.join(_HAMS_COM_DIR, 'hams_shared', 'tools', 'odoo_type_stubs')}\n"
            f"plugins = {os.path.join(_HAMS_COM_DIR, 'hams_shared', 'tools', 'odoo_mypy_plugin.py')}\n",
        )
        _write(os.path.join(self._scratch_dir, "__init__.py"), "from . import models\n")
        _write(
            os.path.join(self._scratch_dir, "__manifest__.py"),
            "{'name': 'mypy plugin class-siblings regression scratch', 'description': 'Scratch fixture for test_odoo_mypy_plugin.py, not a real module.', 'depends': []}\n",
        )
        _write(
            os.path.join(self._scratch_dir, "models", "__init__.py"),
            "from . import primary_model, mixin_model, combiner\n",
        )
        _write(
            os.path.join(self._scratch_dir, "models", "primary_model.py"),
            "from odoo import models\n"
            "\n"
            "\n"
            "class PrimaryModel(models.Model):\n"
            "    _name = 'scratch.regress.model'\n"
            "\n"
            "    def real_only_here(self) -> int:\n"
            "        return 1\n",
        )
        _write(
            os.path.join(self._scratch_dir, "models", "mixin_model.py"),
            "from odoo import models\n"
            "\n"
            "\n"
            "class MixinModel(models.Model):\n"
            "    _name = 'scratch.regress.mixin'\n"
            "\n"
            "    def mixin_only_here(self) -> int:\n"
            "        return 2\n",
        )
        _write(
            os.path.join(self._scratch_dir, "models", "combiner.py"),
            "from odoo import models\n"
            "\n"
            "\n"
            "class Combiner(models.Model):\n"
            "    # No _name -- the mixin self-reference idiom's bare form. This single class is\n"
            "    # a real contributor to BOTH scratch.regress.model and scratch.regress.mixin,\n"
            "    # exactly the shape that triggered the _class_siblings overwrite bug.\n"
            "    _inherit = ['scratch.regress.model', 'scratch.regress.mixin']\n"
            "\n"
            "    def probe(self) -> None:\n"
            "        self.real_only_here()\n"
            "        self.mixin_only_here()\n"
            "\n"
            "    def probe_nonexistent(self) -> None:\n"
            "        self._this_method_genuinely_does_not_exist_anywhere()\n",
        )

    def tearDown(self):
        shutil.rmtree(self._scratch_root, ignore_errors=True)
        shutil.rmtree(self._cache_dir, ignore_errors=True)

    def _run_mypy(self, *files):
        result = subprocess.run(
            [sys.executable, "-m", "mypy", *files, "--config-file", self._ini_path, "--cache-dir", self._cache_dir],
            cwd=_HAMS_COM_DIR,
            env={**os.environ, "ODOO_MYPY_PLUGIN_EXTRA_ROOTS": self._scratch_root},
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.stdout

    def test_a_class_contributing_via_list_inherit_to_two_models_gets_both_siblings(self):
        combiner = os.path.join(self._scratch_dir, "models", "combiner.py")
        primary = os.path.join(self._scratch_dir, "models", "primary_model.py")
        mixin = os.path.join(self._scratch_dir, "models", "mixin_model.py")
        output = self._run_mypy(combiner, primary, mixin)
        self.assertNotIn("real_only_here", output, output)
        self.assertNotIn("mixin_only_here", output, output)

    def test_get_additional_deps_pulls_in_both_targets_without_either_sibling_passed(self):
        combiner = os.path.join(self._scratch_dir, "models", "combiner.py")
        output = self._run_mypy(combiner)
        self.assertNotIn("real_only_here", output, output)
        self.assertNotIn("mixin_only_here", output, output)

    def test_a_genuinely_nonexistent_method_on_the_dual_contributor_is_still_flagged(self):
        combiner = os.path.join(self._scratch_dir, "models", "combiner.py")
        output = self._run_mypy(combiner)
        self.assertIn("_this_method_genuinely_does_not_exist_anywhere", output, output)


if __name__ == "__main__":
    unittest.main()
