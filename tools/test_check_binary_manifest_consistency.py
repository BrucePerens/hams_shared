#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_binary_manifest_consistency.py.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_binary_manifest_consistency as chk  # noqa: E402

_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "check_binary_manifest_consistency.py"
)


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


_XML_HEADER = '<?xml version="1.0" encoding="utf-8"?>\n<odoo><data noupdate="1">\n'
_XML_FOOTER = "</data></odoo>\n"


def _xml_record(name, url, checksum, archive_type, extract_member=None):
    extract_field = (
        f'<field name="extract_member">{extract_member}</field>' if extract_member else ""
    )
    return (
        f'<record id="binary_manifest_{name}" model="binary.manifest">'
        f'<field name="name">{name}</field>'
        f'<field name="url">{url}</field>'
        f'<field name="checksum">{checksum}</field>'
        f'<field name="archive_type">{archive_type}</field>'
        f"{extract_field}"
        "</record>\n"
    )


class CompareTests(unittest.TestCase):
    def test_matching_entries_produce_no_errors(self):
        json_manifest = {
            "kopia": {
                "url": "https://example.com/kopia.tar.gz",
                "checksum": "abc123",
                "type": "tar.gz",
                "extract_member": "kopia",
            }
        }
        xml_manifest = {
            "kopia": {
                "url": "https://example.com/kopia.tar.gz",
                "checksum": "abc123",
                "type": "tar.gz",
                "extract_member": "kopia",
            }
        }
        self.assertEqual(chk.compare(json_manifest, xml_manifest), [])

    def test_url_mismatch_is_reported(self):
        json_manifest = {"kopia": {"url": "https://example.com/new.tar.gz", "checksum": "x", "type": "tar.gz"}}
        xml_manifest = {"kopia": {"url": "https://example.com/old.tar.gz", "checksum": "x", "type": "tar.gz"}}
        errors = chk.compare(json_manifest, xml_manifest)
        self.assertEqual(len(errors), 1)
        self.assertIn("url differs", errors[0])

    def test_checksum_mismatch_is_reported(self):
        json_manifest = {"kopia": {"url": "https://example.com/k.tar.gz", "checksum": "new", "type": "tar.gz"}}
        xml_manifest = {"kopia": {"url": "https://example.com/k.tar.gz", "checksum": "old", "type": "tar.gz"}}
        errors = chk.compare(json_manifest, xml_manifest)
        self.assertEqual(len(errors), 1)
        self.assertIn("checksum differs", errors[0])

    def test_entry_present_only_in_json_is_reported(self):
        json_manifest = {"newtool": {"url": "https://example.com/x", "checksum": "x", "type": "binary"}}
        xml_manifest = {}
        errors = chk.compare(json_manifest, xml_manifest)
        self.assertEqual(len(errors), 1)
        self.assertIn("no matching <record>", errors[0])

    def test_entry_present_only_in_xml_is_reported(self):
        json_manifest = {}
        xml_manifest = {"oldtool": {"url": "https://example.com/x", "checksum": "x", "type": "binary"}}
        errors = chk.compare(json_manifest, xml_manifest)
        self.assertEqual(len(errors), 1)
        self.assertIn("not in binary_manifest.json", errors[0])

    def test_extract_member_only_compared_when_json_declares_one(self):
        # A plain "binary"-type entry (cloudflared) legitimately has no extract_member in
        # either file -- must not be flagged as a mismatch just because both sides are empty,
        # nor because the JSON side omits the key entirely.
        json_manifest = {"cloudflared": {"url": "https://example.com/cf", "checksum": "x", "type": "binary"}}
        xml_manifest = {"cloudflared": {"url": "https://example.com/cf", "checksum": "x", "type": "binary", "extract_member": ""}}
        self.assertEqual(chk.compare(json_manifest, xml_manifest), [])


class MainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        result = subprocess.run(
            [sys.executable, _SCRIPT, self.tmp], capture_output=True, text=True, timeout=30
        )
        return result.returncode, result.stdout + result.stderr

    def _write_manifests(self, json_content, xml_body):
        _write(
            os.path.join(self.tmp, "hams_shared", "tools", "binary_manifest.json"), json_content
        )
        _write(
            os.path.join(self.tmp, "binary_downloader", "data", "binary_manifest_data.xml"),
            _XML_HEADER + xml_body + _XML_FOOTER,
        )

    def test_in_sync_manifests_pass(self):
        self._write_manifests(
            '{"kopia": {"url": "https://example.com/k.tar.gz", "checksum": "abc", '
            '"type": "tar.gz", "extract_member": "kopia"}}',
            _xml_record("kopia", "https://example.com/k.tar.gz", "abc", "tar.gz", "kopia"),
        )
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_drifted_manifests_fail(self):
        # Regression test for the real bug this checker exists for: bumping
        # binary_manifest.json (what dependency_watch.json tracks) without also updating
        # binary_manifest_data.xml (the actual runtime seed data binary_downloader reads).
        self._write_manifests(
            '{"kopia": {"url": "https://example.com/k-NEW.tar.gz", "checksum": "new123", '
            '"type": "tar.gz", "extract_member": "kopia"}}',
            _xml_record("kopia", "https://example.com/k-OLD.tar.gz", "old123", "tar.gz", "kopia"),
        )
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("url differs", out)
        self.assertIn("checksum differs", out)

    def test_missing_manifest_files_are_skipped_not_errored(self):
        # Not every checkout necessarily has both files side by side -- absence is not a
        # consistency violation.
        code, out = self._run()
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
