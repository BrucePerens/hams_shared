# SPDX-License-Identifier: AGPL-3.0-or-later
"""Measurement procedures for a sound card with its output looped back to its input.

Everything is RELATIVE (dBFS at the card's digital input and output).  Absolute volts are never measured; a
loopback cannot tell the DAC's distortion from the ADC's, so the procedures change one stage's operating
point at a time (playback volume, capture gain) and the report reasons from how results move.
"""
from __future__ import annotations

import math

import numpy as np

import dsp


class AlsaBackend:
    def __init__(self, device: str, rate: int = 48000, fmt: str = "S24_3LE"):
        self.device, self.rate, self.fmt = device, rate, fmt

    def duplex(self, stimulus, **kw):
        import alsa_pcm
        kw.setdefault("rate", self.rate)
        return alsa_pcm.run_duplex(self.device, stimulus, fmt=self.fmt, **kw)


class Rig:
    def __init__(self, backend, mixer, play_ctl="Speaker Playback Volume", cap_ctl="Mic Capture Volume",
                 switch_ctl="Speaker Playback Switch", rate=48000, play_channels=(0,), cap_channel=0,
                 period=1024, nperiods=4, log=print):
        self.backend, self.mixer, self.rate = backend, mixer, rate
        self.play_ctl, self.cap_ctl, self.switch_ctl = play_ctl, cap_ctl, switch_ctl
        self.play_channels, self.cap_channel = tuple(play_channels), cap_channel
        self.period, self.nperiods, self.log = period, nperiods, log
        self.gain_db: float | None = None
        self.last_delay: int | None = None
        self.settle_s = 0.2     # discarded at the start of every segment (limiter attack, filter settling)

    # ---- mixer ----
    def _set_db(self, name: str, db: float) -> float:
        c = self.mixer.controls()[name]
        raw = min(max(c.raw_for_db(db), c.vmin), c.vmax)
        self.mixer.set(c.numid, [raw] * max(len(c.values), 1))
        return c.db_at(raw)

    def set_play_db(self, db: float) -> float:
        return self._set_db(self.play_ctl, db)

    def set_cap_db(self, db: float) -> float:
        return self._set_db(self.cap_ctl, db)

    def set_play_raw(self, raw: int) -> None:
        c = self.mixer.controls()[self.play_ctl]
        self.mixer.set(c.numid, [raw] * max(len(c.values), 1))

    def set_cap_raw(self, raw: int) -> None:
        c = self.mixer.controls()[self.cap_ctl]
        self.mixer.set(c.numid, [raw] * max(len(c.values), 1))

    def settings(self) -> dict:
        ctl = self.mixer.controls()
        p, c = ctl[self.play_ctl], ctl[self.cap_ctl]
        return {"play_raw": int(p.values[0]), "play_db": p.db_at(int(p.values[0])),
                "cap_raw": int(c.values[0]), "cap_db": c.db_at(int(c.values[0]))}

    # ---- stimulus helpers ----
    def stereo(self, x: np.ndarray) -> np.ndarray:
        out = np.zeros((len(x), 2))
        for ch in self.play_channels:
            out[:, ch] = x
        return out

    def run(self, mono: np.ndarray, tail_s: float = 0.3, **kw):
        kw.setdefault("period", self.period)
        kw.setdefault("nperiods", self.nperiods)
        return self.backend.duplex(self.stereo(mono), tail_frames=int(tail_s * self.rate), **kw)

    def cap(self, res) -> np.ndarray:
        return res.capture[:, self.cap_channel]

    def steady_tone(self, freq: float, level_dbfs: float, secs: float = 1.2, **kw) -> dict:
        """One steady tone, analyzed over its middle part (no timing alignment needed)."""
        res = self.run(dsp.tone(freq, level_dbfs, secs, self.rate, fade=0.01), **kw)
        x = self.cap(res)
        a, b = int(0.45 * self.rate), int(0.95 * self.rate)
        m = dsp.tone_metrics(x[a:b], self.rate, freq)
        m["play_dbfs"] = level_dbfs
        m["gain_db"] = m["level_dbfs"] - level_dbfs
        m["clip_run"] = dsp.clip_run(x[a:b])
        m["xruns"] = res.xruns_capture + res.xruns_playback
        return m

    def capture_idle(self, secs: float, state: str = "silence") -> np.ndarray:
        """state: stopped (playback closed), silence (digital zeros playing) or muted (output switch off, zeros)."""
        n = int(secs * self.rate)
        if state == "stopped":
            return self.cap(self.backend.duplex(None, capture_only_frames=n, period=self.period, nperiods=self.nperiods))
        if state == "muted":
            self.mixer.set(self.switch_ctl, "off")
        try:
            return self.cap(self.backend.duplex(np.zeros((n, 2)), period=self.period, nperiods=self.nperiods))
        finally:
            if state == "muted":
                self.mixer.set(self.switch_ctl, "on")

    # ---- multi-segment stimulus with a sync marker ----
    def run_segments(self, segs, marker_db: float, lead_s=1.2, marker_s=0.3, gap_s=0.2, tail_s=0.4, fade=0.005):
        """segs: [(freq, level_dbfs, seconds)].  Returns one metrics row per segment."""
        r = self.rate
        marker = dsp.tone(1000.0, marker_db, marker_s, r, fade=0.02)
        parts, layout, pos = [np.zeros(int(lead_s * r)), marker, np.zeros(int(gap_s * r))], [], 0
        pos = sum(len(p) for p in parts)
        for f, lvl, secs in segs:
            t = dsp.tone(f, lvl, secs, r, fade=fade)
            parts.append(t)
            layout.append((pos, pos + len(t)))
            pos += len(t)
        stim = np.concatenate(parts)
        res = self.run(stim, tail_s=tail_s)
        x = self.cap(res)
        k, peak = dsp.matched_delay(marker, x, max(int((lead_s - 0.1) * r), 0), int((lead_s + 0.45) * r))
        delay = k - int(lead_s * r)
        if peak < 0.5 and self.last_delay is not None:
            self.log(f"  sync weak (peak {peak:.2f}); reusing delay {self.last_delay}")
            delay = self.last_delay
        elif peak >= 0.5:
            self.last_delay = delay
        rows = []
        for (f, lvl, secs), (s, e) in zip(segs, layout):
            a, b = s + delay + int(self.settle_s * r), e + delay - int(0.04 * r)
            seg = x[a:b]
            if len(seg) < 2048:
                rows.append({"play_dbfs": lvl, "freq_nominal": f, "error": "window outside capture", "level_dbfs": None})
                continue
            m = dsp.tone_metrics(seg, r, f)
            m.update(play_dbfs=lvl, gain_db=m["level_dbfs"] - lvl, clip_run=dsp.clip_run(seg), freq_nominal=f,
                     sync_peak=peak, delay_frames=delay)
            rows.append(m)
        return rows, res

    def probe_gain(self, level_dbfs: float = -50.0):
        """Small-signal loopback gain (dB) at the current mixer state, or None if the tone is lost in noise."""
        m = self.steady_tone(1000.0, level_dbfs)
        snr = -m["thdn_db"]
        self.gain_db = m["gain_db"] if snr > 12 and m["peak"] < 0.5 else None
        return self.gain_db

    def marker_level(self, hi: float) -> float:
        if self.gain_db is None:
            return min(-6.0, hi)
        return float(min(max(-30.0 - self.gain_db, -60.0), -6.0, hi))


# --------------------------------------------------------------------------------------------- suites
def levels_sweep(rig: Rig, freq: float, lo=-90.0, hi=0.0, step=1.0, seg_s=0.8, block=10, stop_after_clip=3) -> dict:
    """Ascending level staircase at one frequency, in blocks, stopping a few steps after hard clipping starts."""
    rig.probe_gain()
    marker = rig.marker_level(hi)
    levels = [round(lo + i * step, 3) for i in range(int(round((hi - lo) / step)) + 1)]
    rows, clipped = [], 0
    for i in range(0, len(levels), block):
        blk = levels[i:i + block]
        out, _ = rig.run_segments([(freq, l, seg_s) for l in blk], marker_db=marker)
        rig.log(f"  levels {blk[0]}..{blk[-1]} dBFS: delay {out[0].get('delay_frames')} sync {out[0].get('sync_peak')}")
        out = [r for r in out if r.get("level_dbfs") is not None]
        for r in out:
            rows.append(r)
            clipped = clipped + 1 if r["clip_run"] >= 3 else 0
        if clipped >= stop_after_clip:
            break
    return {"freq_hz": freq, "settings": rig.settings(), "probe_gain_db": rig.gain_db, "marker_dbfs": marker,
            "stopped_early": rows[-1]["play_dbfs"] < hi, "rows": rows}


def headroom_summary(rows: list[dict], targets=(-12.0, -20.0)) -> dict:
    """Headroom figures from one level staircase (see the technical report for the definitions)."""
    lev = np.array([r["play_dbfs"] for r in rows])
    cap = np.array([r["level_dbfs"] for r in rows])
    snr = np.array([-r["thdn_db"] for r in rows])
    thd = np.array([r["thd_pct"] for r in rows])
    thdn = np.array([r["thdn_pct"] for r in rows])
    ok = (snr >= 30) & (cap <= -25)
    if not ok.any():
        ok = snr >= max(snr.max() - 6, 10)
    g0 = float(np.median((cap - lev)[ok]))
    out = {"small_signal_gain_db": g0, "dac_fs_over_adc_fs_db": g0, "adc_full_scale_at_play_dbfs": -g0}
    gain = cap - lev
    strong = snr >= 20
    for name, arr, thr in (("thd_0p1", thd, 0.1), ("thd_1", thd, 1.0), ("thd_3", thd, 3.0), ("thdn_1", thdn, 1.0)):
        x = None
        for i in range(len(lev)):
            if strong[i] and arr[i] >= thr:
                x = float(lev[i - 1] + (lev[i] - lev[i - 1]) * (thr - arr[i - 1]) / (arr[i] - arr[i - 1])) if i > 0 and arr[i] != arr[i - 1] else float(lev[i])
                break
        out[f"{name}_play_dbfs"] = x
    for name, drop in (("comp_1db", 1.0), ("comp_3db", 3.0)):
        x = None
        for i in range(len(lev)):
            if strong[i] and gain[i] <= g0 - drop:
                x = float(lev[i])
                break
        out[f"{name}_play_dbfs"] = x
    clip = [float(r["play_dbfs"]) for r in rows if r["clip_run"] >= 3]
    out["hard_clip_play_dbfs"] = clip[0] if clip else None
    out["hard_clip_capture_peak"] = float(rows[[r["clip_run"] >= 3 for r in rows].index(True)]["peak"]) if clip else None
    out["max_clean_capture_dbfs_thd1"] = None
    if out["thd_1_play_dbfs"] is not None:
        out["max_clean_capture_dbfs_thd1"] = out["thd_1_play_dbfs"] + g0
    for t in targets:
        lt = t - g0
        out[f"target_{int(t)}_play_dbfs"] = lt if lev.min() <= lt <= lev.max() else None
        for key in ("thd_0p1", "thd_1", "thd_3", "comp_1db", "hard_clip"):
            x = out.get(f"{key}_play_dbfs")
            out[f"headroom_{int(t)}_to_{key}_db"] = (x - lt) if (x is not None and lev.min() <= lt <= lev.max()) else None
    return out


def noise_run(rig: Rig, secs: float, state: str, nfft: int = 16384) -> dict:
    x = rig.capture_idle(secs + 0.5, state)[int(0.5 * rig.rate):]
    m = dsp.noise_metrics(x, rig.rate, nfft=nfft)
    freqs, p = m.pop("freqs"), m.pop("psd")
    m["spurs"] = [{"freq_hz": f, "over_floor_db": o, "dbfs": d} for f, o, d in dsp.find_spurs(freqs, p, freqs[1])]
    # 1/3-octave band levels for the report
    bands = []
    fc = 20.0
    while fc <= 20000:
        lo, hi = fc / 2 ** (1 / 6), fc * 2 ** (1 / 6)
        sel = (freqs >= lo) & (freqs < hi)
        bands.append({"fc": fc, "dbfs": dsp.db10(float(p[sel].sum()) / dsp.SINE_RMS ** 2) if sel.any() else None})
        fc *= 2 ** (1 / 3)
    m["third_octave"] = bands
    m["state"], m["settings"], m["seconds"] = state, rig.settings(), secs
    m["_freqs"], m["_psd"] = freqs, p
    return m


def thirds(lo=20.0, hi=20000.0, per_octave=3):
    f, out = lo, []
    while f <= hi * 1.001:
        out.append(round(f, 1))
        f *= 2 ** (1 / per_octave)
    return out


def calibrated_play_level(rig: Rig, target_cap_dbfs: float, freq=1000.0) -> float | None:
    """Playback level (dBFS) expected to give target_cap_dbfs at the capture, from a small-signal probe."""
    g = rig.probe_gain()
    if g is None:
        return None
    lvl = target_cap_dbfs - g
    return lvl if lvl <= -0.5 else None


def tones_at(rig: Rig, freqs, play_dbfs: float, secs=0.8) -> list[dict]:
    segs = [(f, play_dbfs, max(secs, 14.0 / f)) for f in freqs]
    rows, _ = rig.run_segments(segs, marker_db=min(play_dbfs, -6.0))
    return rows


def thd_vs_freq(rig: Rig, freqs, target_caps=(-3.0, -10.0, -20.0)) -> dict:
    out = {"settings": rig.settings(), "targets": {}}
    for t in target_caps:
        lvl = calibrated_play_level(rig, t)
        if lvl is None:
            out["targets"][str(t)] = {"play_dbfs": None, "rows": []}
            continue
        out["targets"][str(t)] = {"play_dbfs": lvl, "rows": tones_at(rig, freqs, lvl)}
    return out


def frequency_response(rig: Rig, freqs, target_cap=-20.0) -> dict:
    lvl = calibrated_play_level(rig, target_cap)
    if lvl is None:
        return {"error": "tone not detectable or would need >= 0 dBFS", "rows": []}
    rows = tones_at(rig, freqs, lvl, secs=1.0)
    ref = min(rows, key=lambda r: abs(r["freq_nominal"] - 1000.0))["level_dbfs"]
    for r in rows:
        r["response_db"] = r["level_dbfs"] - ref
    resp = [(r["freq_nominal"], r["response_db"]) for r in rows]
    return {"settings": rig.settings(), "play_dbfs": lvl, "rows": rows, "analysis": response_analysis(resp)}


def response_analysis(resp) -> dict:
    f = np.array([a for a, _ in resp])
    d = np.array([b for _, b in resp])
    out = {}
    lf = hf = None
    i1k = int(np.argmin(np.abs(f - 1000)))
    for i in range(i1k, 0, -1):
        if d[i - 1] <= -3 < d[i] or d[i] <= -3:
            lf = _interp_log(f[i - 1], f[i], d[i - 1], d[i], -3.0) if d[i - 1] < d[i] else f[i]
            break
    for i in range(i1k, len(f) - 1):
        if d[i + 1] <= -3 < d[i] or d[i] <= -3:
            hf = _interp_log(f[i], f[i + 1], d[i], d[i + 1], -3.0) if d[i + 1] < d[i] else f[i]
            break
    out["minus3db_low_hz"], out["minus3db_high_hz"] = (float(lf) if lf else None), (float(hf) if hf else None)
    for name, a, b in (("ssb_300_3000", 300, 3000), ("psk_200_3000", 200, 3000)):
        m = (f >= a) & (f <= b)
        out[f"{name}_ripple_pp_db"] = float(d[m].max() - d[m].min()) if m.any() else None
        out[f"{name}_min_db"], out[f"{name}_max_db"] = (float(d[m].min()), float(d[m].max())) if m.any() else (None, None)
    return out


def _interp_log(f0, f1, d0, d1, target):
    t = (target - d0) / (d1 - d0)
    return math.exp(math.log(f0) + t * (math.log(f1) - math.log(f0)))


def sweep_phase(rig: Rig, target_cap=-20.0, secs=6.0) -> dict:
    lvl = calibrated_play_level(rig, target_cap)
    if lvl is None:
        return {"error": "tone not detectable"}
    sw = dsp.log_sweep(20.0, 20000.0, secs, rig.rate, lvl)
    lead = int(0.5 * rig.rate)
    stim = np.concatenate((np.zeros(lead), sw))
    res = rig.run(stim, tail_s=1.0)
    x = rig.cap(res)
    f, g, ph, gd = dsp.phase_group_delay(stim, x, rig.rate)
    keep = np.unique(np.round(np.logspace(math.log10(20), math.log10(20000), 200)).astype(int))
    idx = np.searchsorted(f, keep)
    idx = idx[idx < len(f)]
    return {"settings": rig.settings(), "play_dbfs": lvl,
            "freq_hz": f[idx].tolist(), "gain_db": g[idx].tolist(), "group_delay_ms": (gd[idx] * 1e3).tolist(),
            "median_group_delay_ms_300_3000": float(np.median(gd[(f >= 300) & (f <= 3000)]) * 1e3)}


def imd_run(rig: Rig, target_cap_peak=-6.0) -> dict:
    out = {"settings": rig.settings()}
    g = rig.probe_gain()
    if g is None:
        return {"error": "tone not detectable"}
    r = rig.rate
    # SMPTE: 60 Hz and 7 kHz at 4:1, summed peak = target
    peak = target_cap_peak - g
    a7 = 10 ** (peak / 20.0) / 5.0
    n = int(2.0 * r)
    t = np.arange(n) / r
    x = 4 * a7 * np.sin(2 * np.pi * 60 * t) + a7 * np.sin(2 * np.pi * 7000 * t)
    x[:480] *= np.linspace(0, 1, 480)
    res = rig.run(x)
    seg = rig.cap(res)[int(0.6 * r):int(1.6 * r)]
    out["smpte"] = dsp.sideband_imd(seg, r, 60.0, 7000.0)
    out["smpte"]["play_peak_dbfs"] = peak
    # CCIF: 19 kHz + 20 kHz equal, summed peak = target
    pk = target_cap_peak - g
    a = 10 ** (pk / 20.0) / 2.0
    y = a * np.sin(2 * np.pi * 19000 * t) + a * np.sin(2 * np.pi * 20000 * t)
    y[:480] *= np.linspace(0, 1, 480)
    res = rig.run(y)
    seg = rig.cap(res)[int(0.6 * r):int(1.6 * r)]
    out["ccif"] = dsp.ccif_imd(seg, r, 19000.0, 20000.0)
    out["ccif"]["play_peak_dbfs"] = pk
    return out


def channels_test(rig: Rig, level_dbfs: float = -20.0) -> dict:
    """Left-only, right-only, both and inverted: which playback channels reach the input, is the input mono."""
    r = rig.rate
    t = dsp.tone(1000.0, level_dbfs, 1.5, r, fade=0.01)
    z = np.zeros_like(t)
    out = {"settings": rig.settings(), "play_dbfs": level_dbfs}
    for name, st in (("L_only", np.stack([t, z], 1)), ("R_only", np.stack([z, t], 1)),
                     ("both", np.stack([t, t], 1)), ("R_inverted", np.stack([t, -t], 1))):
        res = rig.backend.duplex(st, tail_frames=int(0.3 * r), period=rig.period, nperiods=rig.nperiods)
        c = res.capture[int(0.5 * r):int(1.3 * r)]
        out[name] = {"capture_dbfs": [dsp.tone_metrics(c[:, ch], r, 1000.0)["level_dbfs"] for ch in (0, 1)],
                     "capture_channels_identical": bool(np.array_equal(c[:, 0], c[:, 1])),
                     "peak": [float(np.max(np.abs(c[:, ch]))) for ch in (0, 1)]}
    return out


# ------------------------------------------------------------------------------- time-domain suites
def long_run(rig: Rig, seconds: float, play_dbfs: float, freq: float = 1000.0, window_s: float = 5.0, **kw) -> dict:
    """A steady tone for a long time: windowed level / frequency / THD+N drift and the regressed clock rates."""
    r = rig.rate
    amp = 10 ** (play_dbfs / 20.0)

    def src(start, n):
        return rig.stereo(amp * np.sin(2 * np.pi * freq * (start + np.arange(n)) / r))

    store = []      # analysis happens after the run: heavy work inside the loop would starve the playback queue

    def on_chunk(frames, cum):
        store.append(frames[:, rig.cap_channel].astype(np.float32))

    res = rig.backend.duplex(src, total_frames=int(seconds * r), tail_frames=0, keep=False, on_chunk=on_chunk,
                             period=kw.get("period", rig.period), nperiods=kw.get("nperiods", rig.nperiods))
    x = np.concatenate(store).astype(np.float64)
    del store
    windows, wlen = [], int(window_s * r)
    for s in range(0, len(x) - wlen + 1, wlen):
        m = dsp.tone_metrics(x[s:s + wlen], r, freq)
        windows.append({"t_s": (s + wlen) / r, "freq_hz": m["freq_hz"], "level_dbfs": m["level_dbfs"],
                        "thdn_db": m["thdn_db"], "thd_db": m["thd_db"], "peak": m["peak"],
                        "noise_dbfs_per_bin": m["noise_floor_dbfs_per_bin"]})
    skip = max(2, int(2.0 * r / max(res.period, 1)))
    out = {"settings": rig.settings(), "play_dbfs": play_dbfs, "freq_hz": freq, "seconds": seconds,
           "period": res.period, "buffer": res.buffer, "xruns_capture": res.xruns_capture,
           "xruns_playback": res.xruns_playback, "xrun_at_frames": res.xrun_at_frames, "windows": windows}
    if len(res.read_frames) > skip + 10:
        for key, times in (("monotonic", res.read_times), ("monotonic_raw", res.read_times_raw)):
            sl, se = dsp.regress_rate(res.read_frames[skip:], times[skip:])
            out[f"capture_rate_hz_vs_{key}"] = sl
            out[f"capture_rate_se_hz_vs_{key}"] = se
            out[f"capture_ppm_vs_{key}"] = (sl / r - 1) * 1e6
        wf, wt = res.write_frames[skip + rig.nperiods:], res.write_times[skip + rig.nperiods:]
        if len(wf) > 10:
            sl, se = dsp.regress_rate(wf, wt)
            out["playback_rate_hz_vs_monotonic"], out["playback_ppm_vs_monotonic"] = sl, (sl / r - 1) * 1e6
        dt = np.diff(res.read_times[skip:])
        out["read_interval_ms"] = {"mean": float(dt.mean() * 1e3), "std": float(dt.std() * 1e3),
                                   "p99": float(np.percentile(dt, 99) * 1e3), "max": float(dt.max() * 1e3)}
    if windows:
        fs = np.array([w["freq_hz"] for w in windows])
        out["tone_ratio_ppm_median"] = float((np.median(fs) / freq - 1) * 1e6)
        out["tone_ratio_ppm_drift_p2p"] = float((fs.max() - fs.min()) / freq * 1e6)
    return out


def xrun_run(rig: Rig, period: int, nperiods: int, seconds: float, play_dbfs: float = -30.0) -> dict:
    r = rig.rate
    amp = 10 ** (play_dbfs / 20.0)

    def src(start, n):
        return rig.stereo(amp * np.sin(2 * np.pi * 1000.0 * (start + np.arange(n)) / r))

    res = rig.backend.duplex(src, total_frames=int(seconds * r), keep=False, on_chunk=lambda *_: None,
                             period=period, nperiods=nperiods)
    dt = np.diff(res.read_times)
    return {"requested_period": period, "granted_period": res.period, "granted_buffer": res.buffer,
            "nperiods": nperiods, "seconds": seconds, "xruns_capture": res.xruns_capture,
            "xruns_playback": res.xruns_playback, "reads": len(res.read_times),
            "read_interval_ms": {"nominal": res.period / r * 1e3, "mean": float(dt.mean() * 1e3),
                                 "std": float(dt.std() * 1e3), "p99": float(np.percentile(dt, 99) * 1e3),
                                 "max": float(dt.max() * 1e3)}}


def latency_run(rig: Rig, period: int, nperiods: int, ahead: int, play_dbfs: float, nbursts: int = 60,
                spacing_s: float = 0.25) -> dict:
    """Burst round trip DAC->ADC.  stream_ms: capture position minus playback position of the same burst
    (both streams start together); app_ms: from the write() that carried the burst to the return of the
    read() that delivered it (what an application experiences, includes queue depth and period rounding)."""
    r = rig.rate
    burst = dsp.tone(1000.0, play_dbfs, 0.004, r, fade=0.002)
    gap = int(spacing_s * r)
    total = int(0.3 * r) + nbursts * gap + int(0.4 * r)
    stim = np.zeros(total)
    pos = []
    for i in range(nbursts):
        s = int(0.3 * r) + i * gap
        stim[s:s + len(burst)] = burst
        pos.append(s)
    res = rig.backend.duplex(rig.stereo(stim), period=period, nperiods=nperiods, playback_ahead_periods=ahead)
    x = res.capture[:, rig.cap_channel]
    stream, app, hw = [], [], []
    for s in pos:
        seg = x[s:s + int(0.9 * spacing_s * r)]
        if len(seg) < len(burst) + 10:
            continue
        k, q = dsp.matched_delay(burst, seg, 0, len(seg) - len(burst) - 1)
        if q < 0.5:
            continue
        c = np.correlate(seg, burst, "valid")
        if 0 < k < len(c) - 1:
            y0, y1, y2 = c[k - 1], c[k], c[k + 1]
            den = y0 - 2 * y1 + y2
            k = k + (0.5 * (y0 - y2) / den if den else 0.0)
        m = s + k
        stream.append(k / r * 1e3)
        jr = next((j for j, f in enumerate(res.read_frames) if f > m + len(burst)), None)
        jw = next((j for j, f in enumerate(res.write_frames) if f > s), None)
        if jr is not None and jw is not None:
            app.append((res.read_times[jr] - res.write_submit[jw]) * 1e3)
            if res.write_delay and res.write_delay[jw] >= 0:
                # playback queue wait as ALSA reports it, and the burst's offset inside its write chunk
                chunk0 = res.write_frames[jw - 1] if jw else 0
                t_dac = res.write_submit[jw] + (res.write_delay[jw] + (s - chunk0)) / r
                t_adc = res.read_times[jr] - (res.read_frames[jr] - m) / r
                hw.append((t_adc - t_dac) * 1e3)
    st, ap = np.array(stream), np.array(app)
    return {"period": res.period, "buffer": res.buffer, "ahead": ahead, "bursts": len(st),
            "xruns": res.xruns_capture + res.xruns_playback,
            "stream_ms": _stats(st), "app_ms": _stats(ap), "dac_to_adc_est_ms": _stats(np.array(hw))}


def _stats(a: np.ndarray) -> dict:
    if len(a) == 0:
        return {}
    return {"mean": float(a.mean()), "std": float(a.std()), "min": float(a.min()), "max": float(a.max()),
            "p50": float(np.percentile(a, 50)), "p99": float(np.percentile(a, 99))}


def play_volume_sweep(rig: Rig, play_dbfs: float = -40.0, freq: float = 1000.0) -> dict:
    """Level at the capture versus the playback mixer's raw value: dB per step, linearity, mute step."""
    c = rig.mixer.controls()[rig.play_ctl]
    rows = []
    for raw in range(c.vmin, c.vmax + 1):
        rig.set_play_raw(raw)
        m = rig.steady_tone(freq, play_dbfs, secs=0.9)
        rows.append({"raw": raw, "nominal_db": c.db_at(raw), "level_dbfs": m["level_dbfs"], "thdn_db": m["thdn_db"],
                     "peak": m["peak"]})
    return {"control": rig.play_ctl, "play_dbfs": play_dbfs, "rows": rows}


def cap_gain_sweep(rig: Rig, play_dbfs: float, freq: float = 1000.0, noise_s: float = 4.0) -> dict:
    """Tone level and idle noise versus the capture gain: analog gain raises noise with the signal, digital does not."""
    c = rig.mixer.controls()[rig.cap_ctl]
    rows = []
    for raw in range(c.vmin, c.vmax + 1):
        rig.set_cap_raw(raw)
        m = rig.steady_tone(freq, play_dbfs, secs=0.9)
        n = dsp.noise_metrics(rig.capture_idle(noise_s + 0.5, "silence")[int(0.5 * rig.rate):], rig.rate)
        rows.append({"raw": raw, "nominal_db": c.db_at(raw), "level_dbfs": m["level_dbfs"], "thdn_db": m["thdn_db"],
                     "peak": m["peak"], "noise_rms_dbfs": n["rms_dbfs"], "noise_a_dbfs": n["rms_a_dbfs"],
                     "dc": n["dc"]})
    return {"control": rig.cap_ctl, "play_dbfs": play_dbfs, "rows": rows}


# ------------------------------------------------------------------------------------- clicks and pops
def _event_deviation(x: np.ndarray, frame: int, rate: int, pre=(1.5, 0.3), post=(-0.05, 0.6), tone_amp=None) -> dict:
    a, b = max(frame - int(pre[0] * rate), 0), max(frame - int(pre[1] * rate), 1)
    base = x[a:b] if b - a > 1000 else x[frame + int(0.8 * rate):frame + int(1.8 * rate)]
    med = float(np.median(base))
    noise_pk = float(np.max(np.abs(base - med))) if len(base) else 0.0
    win = x[max(frame + int(post[0] * rate), 0):frame + int(post[1] * rate)]
    after = x[frame + int(0.8 * rate):frame + int(1.8 * rate)]
    out = {"baseline_noise_peak_dbfs": dsp.db20(noise_pk), "event_peak_dev_dbfs": dsp.db20(np.max(np.abs(win - med))),
           "dc_step_dbfs": dsp.db20(abs(float(np.median(after)) - med)) if len(after) else None}
    out["event_over_noise_peak_db"] = out["event_peak_dev_dbfs"] - out["baseline_noise_peak_dbfs"]
    return out


def click_run(rig: Rig, seconds: float, events, play_window=None, play_dbfs=None) -> dict:
    """Capture while mixer events (offset_s, label, callable) fire from a helper thread, or while playback
    opens and closes inside play_window (seconds); then the peak deviation around each event."""
    import threading
    import time
    r = rig.rate
    log: list = []

    def worker():
        t0 = time.monotonic()
        for off, label, fn in events:
            dt = t0 + off - time.monotonic()
            if dt > 0:
                time.sleep(dt)
            ts = time.monotonic()
            fn()
            log.append((label, ts))

    th = threading.Thread(target=worker, daemon=True)
    amp = 0.0 if play_dbfs is None else 10 ** (play_dbfs / 20.0)

    def src(start, n):
        return rig.stereo(amp * np.sin(2 * np.pi * 1000.0 * (start + np.arange(n)) / r))

    th.start()
    n = int(seconds * r)
    if play_window:
        res = rig.backend.duplex(src, capture_only_frames=n, total_frames=n, period=rig.period, nperiods=rig.nperiods,
                                 play_window=(int(play_window[0] * r), int(play_window[1] * r)))
    else:
        res = rig.backend.duplex(src, total_frames=n, period=rig.period, nperiods=rig.nperiods)
    th.join(timeout=2)
    x = res.capture[:, rig.cap_channel]
    rf, rt = np.array(res.read_frames, dtype=float), np.array(res.read_times)
    out = []
    for label, ts in log:
        fr = int(np.interp(ts, rt, rf))
        d = _event_deviation(x, fr, r)
        d["label"] = label
        if play_dbfs is not None:
            d["overshoot_ratio"] = _overshoot(x, fr, r)
        out.append(d)
    for key, (fr, ts) in res.events.items():
        d = _event_deviation(x, fr, r)
        d["label"] = key
        out.append(d)
    return {"settings": rig.settings(), "seconds": seconds, "play_dbfs": play_dbfs, "events": out,
            "xruns": res.xruns_capture + res.xruns_playback}


def _overshoot(x, frame, rate):
    before = np.max(np.abs(x[max(frame - int(0.5 * rate), 0):frame - int(0.05 * rate)]))
    after = np.max(np.abs(x[frame + int(0.5 * rate):frame + int(1.0 * rate)]))
    win = np.max(np.abs(x[frame:frame + int(0.1 * rate)]))
    return float(win / max(before, after, 1e-9))


def limiter_dynamics(rig: Rig, low_dbfs: float, high_dbfs: float, freq=1000.0, lead_s=1.5, low1_s=2.0, high_s=3.0,
                     low2_s=4.0, block_ms=5.0) -> dict:
    """Step the tone up (low -> high) and back down: envelope of the capture at block_ms resolution.

    A gain-control or limiter stage shows as an envelope that does not follow the stimulus: attack is the time
    to settle after the up step, release is the time to recover after the down step."""
    r = rig.rate
    parts = [np.zeros(int(lead_s * r)), dsp.tone(freq, low_dbfs, low1_s, r, fade=0.002),
             dsp.tone(freq, high_dbfs, high_s, r, fade=0.002), dsp.tone(freq, low_dbfs, low2_s, r, fade=0.002)]
    res = rig.run(np.concatenate(parts), tail_s=0.6)
    x = rig.cap(res)
    blk = int(block_ms * 1e-3 * r)
    n = len(x) // blk
    env = np.array([dsp.db20(math.sqrt(2.0) * float(np.sqrt(np.mean(x[i * blk:(i + 1) * blk] ** 2)))) for i in range(n)])
    t = (np.arange(n) + 1) * blk / r
    noise = float(np.median(env[:int(0.8 * lead_s * r / blk)]))
    # tone onset: first block 20 dB above the idle level after the lead-in pop has died away
    start_blk = int(0.9 * lead_s * r / blk)
    on = next((i for i in range(start_blk, n) if env[i] > noise + 20.0), None)
    if on is None:
        return {"error": "tone not detected", "settings": rig.settings()}
    t_on = t[on]
    t_up, t_dn = t_on + low1_s, t_on + low1_s + high_s

    def idx(tt):
        return int(round((tt - blk / r) / (blk / r)))

    def med(a, b):
        return float(np.median(env[idx(a):idx(b)]))

    low_before = med(t_up - 0.8, t_up - 0.1)
    high_final = med(t_dn - 1.0, t_dn - 0.1)
    low_after = med(t_dn + low2_s - 1.0, t_dn + low2_s - 0.1)

    def settle(t0, final, window_s, tol):
        for i in range(idx(t0), min(idx(t0 + window_s), n)):
            if abs(env[i] - final) <= tol:
                return float(t[i] - t0) * 1e3
        return None

    up_win = env[idx(t_up):idx(t_up + 0.1)]
    dn_win = env[idx(t_dn):idx(t_dn + 1.5)]
    return {"settings": rig.settings(), "low_dbfs": low_dbfs, "high_dbfs": high_dbfs, "freq_hz": freq,
            "low_capture_before_dbfs": low_before, "high_capture_final_dbfs": high_final,
            "low_capture_after_dbfs": low_after, "expected_step_db": high_dbfs - low_dbfs,
            "observed_step_db": high_final - low_before,
            "attack_to_1db_ms": settle(t_up, high_final, 1.0, 1.0),
            "up_overshoot_db": float(up_win.max() - high_final),
            "release_to_3db_ms": settle(t_dn, low_after, 3.0, 3.0),
            "release_to_1db_ms": settle(t_dn, low_after, 3.0, 1.0),
            "down_undershoot_db": float(dn_win.min() - low_after),
            "t_s": (t - t_on).tolist(), "env_dbfs": env.tolist(),
            "t_up_s": float(low1_s), "t_down_s": float(low1_s + high_s)}
