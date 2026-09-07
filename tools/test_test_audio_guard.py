#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Regression test for test.py's `get_default_audio_sink_name()`, the helper
behind main()'s post-test guard against a real, previously-observed failure
mode: a test-environment browser or subprocess opening the dev box's real
ALSA device directly (bypassing PipeWire's shared access) leaves WirePlumber
stuck on its silent "Dummy Output" fallback even after the offending process
exits, breaking the developer's actual system audio until someone notices by
ear and manually restarts wireplumber.

A first draft of `get_default_audio_sink_name()` stripped only plain ASCII
space/`│` characters from each `wpctl status` line before matching section
headers -- `wpctl`'s tree-drawing prefix actually uses `├`/`└`/`─` box-drawing
characters too, so that draft never matched the "Sinks:" header at all and
silently returned None against real output. Confirmed directly against real
`wpctl status` output on this dev box before and after fixing it. These tests
use captured real-shaped `wpctl status` output (not a hand-abbreviated stub)
so they'd catch the same class of parsing mistake again.

`test.py` is loaded via importlib rather than `import test` because its own
module name collides with Python's stdlib `test` package (matching
test_test_oom_watchdog.py's own established convention for this file).
"""

import importlib.util
import os
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TEST_PY_PATH = os.path.join(_HERE, "test.py")


def _load_test_py_module():
    spec = importlib.util.spec_from_file_location(
        "_hams_test_runner_module_under_test_audio_guard", _TEST_PY_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_test_runner = _load_test_py_module()

# Real `wpctl status` output shape, captured on this dev box while the
# default sink was a genuine hardware speaker.
_REAL_WPCTL_OUTPUT_WITH_REAL_SINK = """PipeWire 'pipewire-0' [1.4.2, bruce@bruce16, cookie:2245492405]
 └─ Clients:
        33. xdg-desktop-portal                  [1.4.2, bruce@bruce16, pid:3116]

Audio
 ├─ Devices:
 │      52. Raptor Lake-P/U/H cAVS              [alsa]
 │
 ├─ Sinks:
 │      59. Raptor Lake-P/U/H cAVS HDMI / DisplayPort 3 Output [vol: 1.00]
 │  *   62. Raptor Lake-P/U/H cAVS Speaker      [vol: 0.41]
 │
 ├─ Sources:
 │  *   64. Raptor Lake-P/U/H cAVS Digital Microphone [vol: 1.00]
 │
 └─ Filters:
"""

# Real shape captured while WirePlumber was stuck on its dummy fallback
# (the exact broken state this guard exists to detect).
_REAL_WPCTL_OUTPUT_WITH_DUMMY_SINK = """PipeWire 'pipewire-0' [1.4.2, bruce@bruce16, cookie:1]
 └─ Clients:
        1. WirePlumber                          [1.4.2, bruce@bruce16, pid:1402]

Audio
 ├─ Devices:
 │
 ├─ Sinks:
 │  *   35. Dummy Output                        [vol: 0.94]
 │
 ├─ Sources:
 │
 └─ Filters:
"""


class GetDefaultAudioSinkNameTests(unittest.TestCase):
    def _with_fake_wpctl(self, stdout, returncode=0):
        return mock.patch.object(
            _test_runner.subprocess,
            "run",
            return_value=mock.Mock(returncode=returncode, stdout=stdout),
        )

    def test_extracts_the_real_default_sink_name(self):
        with self._with_fake_wpctl(_REAL_WPCTL_OUTPUT_WITH_REAL_SINK):
            name = _test_runner.get_default_audio_sink_name()
        self.assertEqual(name, "Raptor Lake-P/U/H cAVS Speaker")

    def test_extracts_dummy_output_when_stuck_on_the_fallback(self):
        with self._with_fake_wpctl(_REAL_WPCTL_OUTPUT_WITH_DUMMY_SINK):
            name = _test_runner.get_default_audio_sink_name()
        self.assertEqual(name, "Dummy Output")

    def test_returns_none_when_wpctl_is_not_installed(self):
        with mock.patch.object(
            _test_runner.subprocess, "run", side_effect=FileNotFoundError
        ):
            self.assertIsNone(_test_runner.get_default_audio_sink_name())

    def test_returns_none_on_nonzero_exit(self):
        with self._with_fake_wpctl("", returncode=1):
            self.assertIsNone(_test_runner.get_default_audio_sink_name())


if __name__ == "__main__":
    unittest.main()
