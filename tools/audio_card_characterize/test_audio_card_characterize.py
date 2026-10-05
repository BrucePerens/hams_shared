# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests on synthetic signals and the simulated card.  No sound hardware is touched.

Run:  python3 -m unittest tools/audio_card_characterize/test_audio_card_characterize.py
"""
from __future__ import annotations

import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alsa_pcm  # noqa: E402
import dsp  # noqa: E402
import measure  # noqa: E402
from mixer import Control  # noqa: E402
from sim import SimBackend, SimMixer  # noqa: E402

R = 48000


def sim_rig(**kw):
    mixer = SimMixer()
    rig = measure.Rig(SimBackend(mixer, **kw), mixer)
    rig.set_play_db(0.0)
    rig.set_cap_db(0.5)
    return rig


class DspTests(unittest.TestCase):
    def test_a_weighting_reference_values(self):
        a = dsp.a_weight_db([100.0, 1000.0, 10000.0])
        self.assertAlmostEqual(a[1], 0.0, delta=0.05)
        self.assertAlmostEqual(a[0], -19.1, delta=0.1)
        self.assertAlmostEqual(a[2], -2.5, delta=0.1)

    def test_tone_level_is_peak_referenced(self):
        m = dsp.tone_metrics(dsp.tone(1000.0, -20.0, 1.0, R), R, 1000.0)
        self.assertAlmostEqual(m["level_dbfs"], -20.0, delta=0.02)

    def test_frequency_fit_finds_ppm_offsets(self):
        f = 1000.0 * (1 + 37e-6)
        x = dsp.tone(f, -10.0, 4.0, R)
        got, _, _ = dsp.fit_tone(x, R, 1000.0)
        self.assertAlmostEqual((got / 1000.0 - 1) * 1e6, 37.0, delta=0.5)

    def test_thd_of_known_harmonics(self):
        t = np.arange(R) / R
        x = np.sin(2 * np.pi * 1000 * t) + 0.01 * np.sin(2 * np.pi * 2000 * t) + 0.001 * np.sin(2 * np.pi * 3000 * t)
        x *= 0.5
        m = dsp.tone_metrics(x, R, 1000.0)
        want = 100 * math.sqrt(0.01 ** 2 + 0.001 ** 2)
        self.assertAlmostEqual(m["thd_pct"], want, delta=0.01 * want)
        self.assertAlmostEqual(m["harmonics_db"][0], -40.0, delta=0.1)
        self.assertAlmostEqual(m["harmonics_db"][1], -60.0, delta=0.2)

    def test_thdn_includes_noise_and_thd_does_not(self):
        rng = np.random.default_rng(3)
        x = dsp.tone(1000.0, -10.0, 2.0, R) + rng.normal(0, 10 ** (-70 / 20), 2 * R)
        m = dsp.tone_metrics(x, R, 1000.0)
        self.assertLess(m["thd_db"], -90.0)           # no harmonics: noise must not leak into THD
        want = 10 * math.log10(1e-7 * (20000 - 20) / (R / 2) / 0.05)    # in-band noise power over tone power
        self.assertAlmostEqual(m["thdn_db"], want, delta=0.5)

    def test_noise_metrics_white_noise(self):
        rng = np.random.default_rng(5)
        x = rng.normal(0, 10 ** (-60 / 20), 10 * R)
        n = dsp.noise_metrics(x, R)
        inband = -60 + 10 * math.log10((20000 - 20) / (R / 2))      # re peak-1.0 RMS
        self.assertAlmostEqual(n["rms_raw_dbfs_peakref"], inband, delta=0.3)
        self.assertAlmostEqual(n["rms_dbfs"], inband + 3.0103, delta=0.3)
        self.assertLess(n["rms_a_dbfs"], n["rms_dbfs"])               # A-weighting trims LF and HF white noise

    def test_dc_offset(self):
        n = dsp.noise_metrics(np.full(4 * R, 0.01) + np.random.default_rng(1).normal(0, 1e-4, 4 * R), R)
        self.assertAlmostEqual(n["dc"], 0.01, delta=1e-5)

    def test_spur_finder_sees_a_mains_hum_line(self):
        rng = np.random.default_rng(7)
        t = np.arange(10 * R) / R
        x = rng.normal(0, 1e-4, len(t)) + 1e-3 * np.sin(2 * np.pi * 60.0 * t)
        n = dsp.noise_metrics(x, R)
        spurs = dsp.find_spurs(n["freqs"], n["psd"], n["freqs"][1])
        self.assertTrue(any(abs(s[0] - 60.0) < 4 for s in spurs))
        top = max(spurs, key=lambda s: s[1])
        self.assertAlmostEqual(top[2], dsp.db20(1e-3), delta=1.5)

    def test_smpte_imd_of_a_known_sideband(self):
        t = np.arange(R) / R
        x = 0.4 * np.sin(2 * np.pi * 60 * t) + 0.1 * np.sin(2 * np.pi * 7000 * t) + 0.001 * np.sin(2 * np.pi * 7060 * t)
        got = dsp.sideband_imd(x, R, 60.0, 7000.0)
        self.assertAlmostEqual(got["imd_pct"], 1.0, delta=0.05)

    def test_ccif_products(self):
        t = np.arange(R) / R
        x = 0.25 * np.sin(2 * np.pi * 19000 * t) + 0.25 * np.sin(2 * np.pi * 20000 * t) + 0.005 * np.sin(2 * np.pi * 1000 * t)
        got = dsp.ccif_imd(x, R, 19000.0, 20000.0)
        self.assertAlmostEqual(got["d2_pct"], 1.0, delta=0.05)

    def test_clip_run(self):
        x = np.clip(2.0 * dsp.tone(1000.0, 0.0, 0.1, R), -1.0, 1.0)
        self.assertGreater(dsp.clip_run(x), 5)
        self.assertEqual(dsp.clip_run(0.5 * dsp.tone(1000.0, 0.0, 0.1, R)), 0)

    def test_rate_regression_recovers_ppm(self):
        n = np.arange(1, 2000) * 1024
        times = n / (R * (1 + 25e-6))
        slope, se = dsp.regress_rate(n, times)
        self.assertAlmostEqual((slope / R - 1) * 1e6, 25.0, delta=0.01)
        self.assertLess(se, 1e-3)

    def test_matched_delay_finds_a_marker_in_noise(self):
        rng = np.random.default_rng(2)
        marker = dsp.tone(1000.0, -10.0, 0.3, R, fade=0.02)
        rec = rng.normal(0, 1e-3, 3 * R)
        rec[20000:20000 + len(marker)] += 0.2 * marker
        rec[60000:60000 + len(marker)] += 2.0 * marker           # a louder copy later must not win in a window
        k, q = dsp.matched_delay(marker, rec, 10000, 30000)
        self.assertEqual(k, 20000)
        self.assertGreater(q, 0.9)

    def test_group_delay_of_a_pure_delay(self):
        sw = dsp.log_sweep(20.0, 20000.0, 2.0, R, -10.0)
        rec = np.concatenate((np.zeros(96), sw))[:len(sw) + 200]   # 96 frames = 2 ms
        f, g, ph, gd = dsp.phase_group_delay(sw, rec, R)
        self.assertAlmostEqual(float(np.median(gd[(f > 300) & (f < 3000)])) * 1e3, 2.0, delta=0.05)
        self.assertAlmostEqual(float(np.median(g)), 0.0, delta=0.2)

    def test_crossing_interpolation(self):
        self.assertAlmostEqual(dsp.interp_crossing([0, 10], [0.0, 2.0], 1.0), 5.0)
        self.assertIsNone(dsp.interp_crossing([0, 10], [0.0, 0.5], 1.0))


class CodecTests(unittest.TestCase):
    def test_s24_3le_round_trip(self):
        x = np.stack([dsp.tone(1000.0, -3.0, 0.05, R), -dsp.tone(500.0, -9.0, 0.05, R)], axis=1)
        back = alsa_pcm.decode(alsa_pcm.encode(x, "S24_3LE"), "S24_3LE")
        self.assertLess(float(np.max(np.abs(back - x))), 1.0 / 8388608)

    def test_s16_round_trip_and_clip(self):
        x = np.array([[0.0, 1.5], [-1.5, 0.25]])
        back = alsa_pcm.decode(alsa_pcm.encode(x, "S16_LE"), "S16_LE")
        self.assertAlmostEqual(back[0, 1], 32767 / 32768)
        self.assertAlmostEqual(back[1, 0], -1.0)
        self.assertAlmostEqual(back[1, 1], 0.25)


class MixerTests(unittest.TestCase):
    def test_db_scale(self):
        c = Control(8, "Speaker Playback Volume", "INTEGER", 0, 88, -44.0, 0.0, ["41", "41"])
        self.assertAlmostEqual(c.db_at(41), -23.5)
        self.assertEqual(c.raw_for_db(-23.5), 41)
        self.assertEqual(c.raw_for_db(0.0), 88)


class SimulatedCardTests(unittest.TestCase):
    def test_steady_tone_gain_matches_the_model(self):
        rig = sim_rig(cable_db=6.0)
        m = rig.steady_tone(1000.0, -30.0)
        self.assertAlmostEqual(m["gain_db"], 6.0 + 0.5, delta=0.1)

    def test_staircase_finds_adc_clipping_and_headroom(self):
        rig = sim_rig(cable_db=6.0, noise_dbfs=-95.0)
        r = measure.levels_sweep(rig, 1000.0, lo=-80.0, hi=0.0, step=2.0, seg_s=0.5)
        s = measure.headroom_summary(r["rows"])
        self.assertAlmostEqual(s["small_signal_gain_db"], 6.5, delta=0.3)
        # ADC clips when play + 6.5 dB reaches 0 dBFS: at about -6.5 dBFS playback
        self.assertIsNotNone(s["hard_clip_play_dbfs"])
        self.assertAlmostEqual(s["hard_clip_play_dbfs"], -6.0, delta=2.1)
        self.assertGreater(s["dac_fs_over_adc_fs_db"], 0.0)       # DAC full scale exceeds ADC full scale
        self.assertAlmostEqual(s["headroom_-20_to_hard_clip_db"], 20.0, delta=3.0)   # -26.5 to -6.5 dBFS playback

    def test_dac_distortion_is_visible_when_the_adc_has_room(self):
        rig = sim_rig(cable_db=-20.0, k3=0.2, noise_dbfs=-100.0)
        r = measure.levels_sweep(rig, 1000.0, lo=-60.0, hi=0.0, step=3.0, seg_s=0.5)
        s = measure.headroom_summary(r["rows"])
        self.assertIsNone(s["hard_clip_play_dbfs"])               # ADC never clips here
        self.assertIsNotNone(s["thd_1_play_dbfs"])                # but the cubic term crosses 1% THD
        self.assertGreater(s["thd_0p1_play_dbfs"], -60.0)
        self.assertLess(s["thd_0p1_play_dbfs"], s["thd_1_play_dbfs"])

    def test_clip_point_moves_with_capture_gain_when_the_adc_is_the_limit(self):
        points = []
        for cap_db in (0.5, 10.0):
            rig = sim_rig(cable_db=2.0, noise_dbfs=-100.0)
            rig.set_cap_db(cap_db)
            r = measure.levels_sweep(rig, 1000.0, lo=-60.0, hi=0.0, step=2.0, seg_s=0.5)
            points.append(measure.headroom_summary(r["rows"])["hard_clip_play_dbfs"])
        self.assertAlmostEqual(points[0] - points[1], 9.5, delta=2.1)

    def test_noise_run_reports_levels_and_dc(self):
        rig = sim_rig(noise_dbfs=-80.0, dc=0.002)
        m = measure.noise_run(rig, 4.0, "silence")
        self.assertAlmostEqual(m["dc"], 0.002, delta=1e-4)
        self.assertLess(m["rms_a_dbfs"], m["rms_dbfs"])

    def test_muted_state_restores_the_switch(self):
        rig = sim_rig()
        measure.noise_run(rig, 2.0, "muted")
        self.assertEqual(rig.mixer.ctls["Speaker Playback Switch"].values[0], "on")

    def test_long_run_measures_a_clock_error(self):
        rig = sim_rig(ppm=40.0, noise_dbfs=-100.0)
        out = measure.long_run(rig, 30.0, -20.0, window_s=5.0)
        self.assertAlmostEqual(out["capture_ppm_vs_monotonic"], 40.0, delta=1.0)   # frames arrive 40 ppm fast
        self.assertGreater(len(out["windows"]), 3)

    def test_response_analysis_finds_minus_3db_points(self):
        f = np.array(measure.thirds(20, 20000, 6))
        d = -10 * np.log10(1 + (80.0 / f) ** 2) - 10 * np.log10(1 + (f / 15000.0) ** 4)
        a = measure.response_analysis(list(zip(f, d)))
        self.assertAlmostEqual(a["minus3db_low_hz"], 80.0, delta=6.0)
        self.assertAlmostEqual(a["minus3db_high_hz"], 15000.0, delta=1200.0)

    def test_channels_test_reports_a_mono_input(self):
        rig = sim_rig()
        out = measure.channels_test(rig)
        self.assertTrue(out["L_only"]["capture_channels_identical"])
        self.assertGreater(out["L_only"]["capture_dbfs"][0], out["R_only"]["capture_dbfs"][0] + 60)


if __name__ == "__main__":
    unittest.main()
