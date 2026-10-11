#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_doc_images.py.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import check_doc_images as chk  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_doc_images.py")
FAKE_BYTES = b"fake nexus erp mock-up"
REAL_BYTES = b"a real screenshot of this module"


def _write(path, content, binary=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if binary else "w", **({} if binary else {"encoding": "utf-8"})) as f:
        f.write(content)


class CheckDocImagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.deny = dict(chk.DENYLIST)
        import hashlib

        chk.DENYLIST[hashlib.md5(FAKE_BYTES).hexdigest()] = "test fake"

    def tearDown(self):
        chk.DENYLIST.clear()
        chk.DENYLIST.update(self.deny)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _doc(self, module, src, name="documentation.html"):
        _write(
            os.path.join(self.tmp, module, "data", name),
            f'<div><img src="{src}" alt="x" /></div>',
        )

    def test_a_real_image_is_accepted(self):
        _write(os.path.join(self.tmp, "mod_a", "static", "description", "images", "ui.png"), REAL_BYTES, True)
        self._doc("mod_a", "/mod_a/static/description/images/ui.png")
        self.assertEqual(chk.check_doc_images(self.tmp), [])

    def test_the_scaffolding_file_name_is_rejected_even_with_new_bytes(self):
        _write(os.path.join(self.tmp, "mod_a", "static", "description", "images", "form_screenshot.png"), REAL_BYTES, True)
        self._doc("mod_a", "/mod_a/static/description/images/form_screenshot.png")
        found = chk.check_doc_images(self.tmp)
        self.assertEqual(len(found), 1)
        self.assertIn("placeholder name", found[0])

    def test_a_denylisted_md5_is_rejected_under_any_file_name(self):
        _write(os.path.join(self.tmp, "mod_a", "static", "src", "img", "pretty.png"), FAKE_BYTES, True)
        self._doc("mod_a", "/mod_a/static/src/img/pretty.png")
        found = chk.check_doc_images(self.tmp)
        self.assertEqual(len(found), 1)
        self.assertIn("test fake", found[0])

    def test_a_missing_image_is_rejected(self):
        self._doc("mod_a", "/mod_a/static/description/images/gone.png")
        found = chk.check_doc_images(self.tmp)
        self.assertEqual(len(found), 1)
        self.assertIn("does not exist", found[0])

    def test_external_and_data_urls_are_ignored(self):
        self._doc("mod_a", "https://example.com/x.png")
        self._doc("mod_b", "data:image/png;base64,AAAA")
        self.assertEqual(chk.check_doc_images(self.tmp), [])

    def test_the_same_image_in_three_modules_is_a_copied_placeholder(self):
        for module in ("mod_a", "mod_b", "mod_c"):
            _write(os.path.join(self.tmp, module, "static", "description", "images", "ui.png"), REAL_BYTES, True)
            self._doc(module, f"/{module}/static/description/images/ui.png")
        found = chk.check_doc_images(self.tmp)
        self.assertEqual(len(found), 1)
        self.assertIn("Identical image", found[0])

    def test_two_modules_sharing_an_image_is_allowed(self):
        for module in ("mod_a", "mod_b"):
            _write(os.path.join(self.tmp, module, "static", "description", "images", "ui.png"), REAL_BYTES, True)
            self._doc(module, f"/{module}/static/description/images/ui.png")
        self.assertEqual(chk.check_doc_images(self.tmp), [])

    def test_a_sibling_repo_image_resolves_through_extra_roots(self):
        sibling = tempfile.mkdtemp()
        try:
            _write(os.path.join(sibling, "mod_x", "static", "ui.png"), REAL_BYTES, True)
            self._doc("mod_a", "/mod_x/static/ui.png")
            self.assertEqual(chk.check_doc_images(self.tmp, [sibling]), [])
        finally:
            shutil.rmtree(sibling, ignore_errors=True)

    def test_readme_markdown_images_are_checked(self):
        _write(os.path.join(self.tmp, "mod_a", "README.md"), "![shot](static/description/images/form_screenshot.png)\n")
        found = chk.check_doc_images(self.tmp)
        self.assertEqual(len(found), 1)
        self.assertIn("placeholder name", found[0])

    def test_files_that_are_not_documentation_are_not_scanned(self):
        _write(os.path.join(self.tmp, "mod_a", "views", "page.html"), '<img src="/mod_a/form_screenshot.png">')
        self.assertEqual(chk.check_doc_images(self.tmp), [])

    def test_cli_exit_status_and_denylist_entries_are_full_md5s(self):
        self._doc("mod_a", "/mod_a/static/gone.png")
        res = subprocess.run([sys.executable, _SCRIPT, self.tmp], capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("does not exist", res.stdout)
        for digest in self.deny:
            self.assertRegex(digest, r"^[0-9a-f]{32}$")


if __name__ == "__main__":
    unittest.main()
