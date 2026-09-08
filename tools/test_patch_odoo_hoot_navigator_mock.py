#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for patch_odoo_hoot_navigator_mock.py's pure content-patching logic."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import patch_odoo_hoot_navigator_mock as patcher  # noqa: E402

_REAL_SHAPED_SNIPPET = """export const mockNavigator = createMock(navigator, {
    clipboard: { value: mockClipboard },
    maxTouchPoints: { get: () => (globalThis.ontouchstart === undefined ? 0 : 1) },
    permissions: { value: mockPermissions },
    sendBeacon: { get: () => mockValues.sendBeacon },
    serviceWorker: { value: mockServiceWorker },
    userAgent: { get: () => mockValues.userAgent },
    vibrate: { get: () => mockValues.vibrate },
});
"""


class AlreadyPatchedTests(unittest.TestCase):
    def test_unpatched_content_is_not_already_patched(self):
        self.assertFalse(patcher.already_patched(_REAL_SHAPED_SNIPPET))

    def test_patched_content_is_detected(self):
        patched = patcher.apply_patch(_REAL_SHAPED_SNIPPET)
        self.assertTrue(patcher.already_patched(patched))


class ApplyPatchTests(unittest.TestCase):
    def test_inserts_a_platform_override_right_after_vibrate(self):
        patched = patcher.apply_patch(_REAL_SHAPED_SNIPPET)
        self.assertIn("platform: { get: () => navigator.platform },", patched)
        self.assertLess(
            patched.index("vibrate: { get: () => mockValues.vibrate },"),
            patched.index("platform: { get: () => navigator.platform },"),
        )
        # Nothing else in the snippet should be touched.
        self.assertIn("clipboard: { value: mockClipboard },", patched)
        self.assertIn("userAgent: { get: () => mockValues.userAgent },", patched)

    def test_applying_twice_is_idempotent(self):
        once = patcher.apply_patch(_REAL_SHAPED_SNIPPET)
        twice = patcher.apply_patch(once)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("platform: { get: () => navigator.platform },"), 1)

    def test_raises_when_the_anchor_line_is_missing(self):
        with self.assertRaises(ValueError):
            patcher.apply_patch("some completely different file content")

    def test_raises_when_the_anchor_line_appears_more_than_once(self):
        duplicated = _REAL_SHAPED_SNIPPET + _REAL_SHAPED_SNIPPET
        with self.assertRaises(ValueError):
            patcher.apply_patch(duplicated)


if __name__ == "__main__":
    unittest.main()
