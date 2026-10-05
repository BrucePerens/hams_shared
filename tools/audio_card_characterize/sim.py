# SPDX-License-Identifier: AGPL-3.0-or-later
"""A software stand-in for a sound card in loopback, used by the unit tests (and handy for dry runs).

Signal path: digital -> playback volume -> DAC full-scale clip -> optional cubic distortion -> cable gain ->
capture gain -> ADC clip -> noise, DC, delay.  Playback channel 0 only is wired to the (mono) input.
"""
from __future__ import annotations

import numpy as np

from alsa_pcm import DuplexResult
from mixer import Control


class SimMixer:
    def __init__(self):
        self.ctls = {
            "Speaker Playback Volume": Control(8, "Speaker Playback Volume", "INTEGER", 0, 88, -44.0, 0.0, ["88", "88"]),
            "Mic Capture Volume": Control(4, "Mic Capture Volume", "INTEGER", 0, 60, 0.0, 30.0, ["1", "1"]),
            "Speaker Playback Switch": Control(7, "Speaker Playback Switch", "BOOLEAN", None, None, None, None, ["on"]),
        }

    def controls(self):
        return self.ctls

    def set(self, name_or_numid, value):
        for c in self.ctls.values():
            if c.numid == name_or_numid or c.name == name_or_numid:
                c.values = [str(v) for v in (value if isinstance(value, (list, tuple)) else [value])]

    def get(self, name):
        return self.ctls[name].values

    def snapshot(self):
        return {c.numid: ",".join(c.values) for c in self.ctls.values()}

    def restore(self, snap):
        for numid, val in snap.items():
            self.set(numid, val.split(","))


class SimBackend:
    def __init__(self, mixer: SimMixer, cable_db=6.0, dac_clip=1.0, adc_clip=1.0, k3=0.0, noise_dbfs=-90.0,
                 dc=0.0, delay=480, ppm=0.0, seed=1):
        self.mixer, self.cable_db, self.dac_clip, self.adc_clip, self.k3 = mixer, cable_db, dac_clip, adc_clip, k3
        self.noise_dbfs, self.dc, self.delay, self.ppm = noise_dbfs, dc, delay, ppm
        self.rng = np.random.default_rng(seed)

    def _db(self, name):
        c = self.mixer.ctls[name]
        return c.db_at(int(c.values[0]))

    def duplex(self, stimulus, rate=48000, period=1024, nperiods=4, tail_frames=0, capture_only_frames=0,
               total_frames=None, keep=True, on_chunk=None, **_) -> DuplexResult:
        if capture_only_frames:
            x = np.zeros(capture_only_frames)
        else:
            arr = stimulus(0, int(total_frames)) if callable(stimulus) else np.asarray(stimulus, dtype=np.float64)
            x = np.concatenate((arr[:, 0], np.zeros(tail_frames)))
        n = len(x)
        muted = self.mixer.ctls["Speaker Playback Switch"].values[0] == "off"
        cap_raw = int(self.mixer.ctls["Mic Capture Volume"].values[0])
        y = x * 10 ** (self._db("Speaker Playback Volume") / 20.0) * (0.0 if muted else 1.0)
        y = np.clip(y, -self.dac_clip, self.dac_clip)
        y = y + self.k3 * y ** 3
        y = y * 10 ** (self.cable_db / 20.0)
        y = y * (0.0 if cap_raw == 0 else 10 ** (self._db("Mic Capture Volume") / 20.0))
        y = np.concatenate((np.zeros(self.delay), y))[:n]
        if cap_raw:
            y = y + self.rng.normal(0, 10 ** (self.noise_dbfs / 20.0), n)
        y = np.clip(y + self.dc, -self.adc_clip, self.adc_clip)
        cap = np.stack([y, y], axis=1)
        res = DuplexResult(cap if keep else np.zeros((0, 2)), rate, period, period * nperiods)
        eff = rate * (1 + self.ppm * 1e-6)
        for s in range(0, n, period):
            e = min(n, s + period)
            res.read_frames.append(e)
            res.read_times.append(e / eff)
            res.read_times_raw.append(e / eff)
            res.write_frames.append(e)
            res.write_times.append(e / eff)
            res.write_submit.append(s / eff)
            if on_chunk:
                on_chunk(cap[s:e], e)
        return res
