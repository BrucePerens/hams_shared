# SPDX-License-Identifier: AGPL-3.0-or-later
"""Signal analysis for audio-card characterization.  numpy only.

Level conventions (AES17 style): a full-scale sine of peak 1.0 is 0 dBFS.  Tone levels are reported as
sine peak re 1.0 (equal to RMS re the RMS of a full-scale sine).  Noise levels are RMS re the RMS of a
full-scale sine, so noise at 1/sqrt(2) linear RMS would read 0 dBFS.
"""
from __future__ import annotations

import math

import numpy as np

SINE_RMS = 1.0 / math.sqrt(2.0)


def db20(x) -> float:
    return 20.0 * math.log10(max(float(x), 1e-300))


def db10(x) -> float:
    return 10.0 * math.log10(max(float(x), 1e-300))


def window_bh4(n: int) -> np.ndarray:
    """4-term Blackman-Harris window (sidelobes -92 dB)."""
    k = np.arange(n) * (2.0 * np.pi / n)
    return 0.35875 - 0.48829 * np.cos(k) + 0.14128 * np.cos(2 * k) - 0.01168 * np.cos(3 * k)


def a_weight_db(f) -> np.ndarray:
    """IEC 61672 A-weighting in dB (0 dB at 1 kHz)."""
    f = np.asarray(f, dtype=np.float64)
    f2 = np.maximum(f, 1e-3) ** 2
    ra = (12194.0 ** 2 * f2 ** 2) / ((f2 + 20.6 ** 2) * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2)) * (f2 + 12194.0 ** 2))
    return 20.0 * np.log10(ra) + 2.0


def tone(freq: float, level_dbfs: float, seconds: float, rate: int, phase: float = 0.0, fade: float = 0.0) -> np.ndarray:
    n = int(round(seconds * rate))
    t = np.arange(n) / rate
    x = 10.0 ** (level_dbfs / 20.0) * np.sin(2 * np.pi * freq * t + phase)
    if fade > 0:
        m = min(int(fade * rate), n // 2)
        r = 0.5 - 0.5 * np.cos(np.pi * np.arange(m) / m)
        x[:m] *= r
        x[n - m:] *= r[::-1]
    return x


def power_spectrum(x: np.ndarray, rate: int):
    """One-sided power per bin with the BH4 window.  The sum over bins equals the mean square of x."""
    n = len(x)
    w = window_bh4(n)
    spec = np.fft.rfft((x - np.mean(x)) * w)
    p = (np.abs(spec) ** 2) / (n * np.sum(w ** 2))
    p[1:] *= 2.0
    if n % 2 == 0:
        p[-1] /= 2.0
    return np.fft.rfftfreq(n, 1.0 / rate), p


def _bin_power(freqs, p, f, half_hz):
    m = (freqs >= f - half_hz) & (freqs <= f + half_hz)
    return float(np.sum(p[m]))


def fit_tone(x: np.ndarray, rate: int, f_guess: float, search_hz: float | None = None):
    """Frequency (Hz), peak amplitude and phase of the dominant tone near f_guess (interpolated DFT peak)."""
    n = len(x)
    w = window_bh4(n)
    xw = (x - np.mean(x)) * w
    sp = np.fft.rfft(xw)
    df = rate / n
    search = search_hz if search_hz is not None else max(10 * df, 0.02 * f_guess)
    lo, hi = max(int((f_guess - search) / df), 1), min(int((f_guess + search) / df) + 1, len(sp) - 1)
    k = lo + int(np.argmax(np.abs(sp[lo:hi])))

    def mag(f):
        t = np.arange(n) / rate
        return abs(np.sum(xw * np.exp(-2j * np.pi * f * t)))

    a, b = (k - 1) * df, (k + 1) * df
    g = (math.sqrt(5) - 1) / 2
    c, d = b - g * (b - a), a + g * (b - a)
    for _ in range(40):
        if mag(c) > mag(d):
            b = d
        else:
            a = c
        c, d = b - g * (b - a), a + g * (b - a)
        if abs(b - a) < 1e-7 * max(f_guess, 1):
            break
    f = (a + b) / 2
    t = np.arange(n) / rate
    z = np.sum(xw * np.exp(-2j * np.pi * f * t))
    return f, 2.0 * abs(z) / float(np.sum(w)), float(np.angle(z))


def noise_per_bin(freqs, p, exclude, lo_hz, hi_hz):
    """Robust mean noise power per bin: median of bins in the band, corrected for the exponential spread."""
    m = (freqs >= lo_hz) & (freqs <= hi_hz)
    for f, half in exclude:
        m &= ~((freqs >= f - half) & (freqs <= f + half))
    if not np.any(m):
        return 0.0
    return float(np.median(p[m]) / math.log(2.0))


def tone_metrics(x: np.ndarray, rate: int, f_guess: float, band=(20.0, 20000.0), n_harm: int = 10) -> dict:
    """Level, THD (harmonics only, noise-corrected), THD+N and harmonic list of a single steady tone."""
    f0, amp, _ = fit_tone(x, rate, f_guess)
    freqs, p = power_spectrum(x, rate)
    df = freqs[1]
    half = 4.5 * df
    total = float(np.sum(p[(freqs >= band[0]) & (freqs <= band[1])]))
    fund = _bin_power(freqs, p, f0, half)
    harm_freqs = [h * f0 for h in range(2, n_harm + 1) if h * f0 <= min(band[1], rate / 2 - 5 * df)]
    excl = [(f0, half)] + [(hf, half) for hf in harm_freqs]
    nb = noise_per_bin(freqs, p, excl, max(band[0], 20.0), band[1])
    nbins = int(2 * half / df) + 1
    harm_pw = []
    for hf in harm_freqs:
        raw = _bin_power(freqs, p, hf, half)
        # a harmonic counts only when it stands 6 dB above the noise in its own bins; otherwise noise would bias THD up
        harm_pw.append(raw - nb * nbins if raw > 4.0 * nb * nbins else 0.0)
    thd_pw = float(sum(harm_pw))
    thdn_pw = max(total - fund, 0.0)
    return {
        "freq_hz": f0,
        "level_dbfs": db20(amp),
        "thd_db": db10(thd_pw / fund) if fund > 0 else 0.0,
        "thd_pct": 100.0 * math.sqrt(thd_pw / fund) if fund > 0 else 0.0,
        "thdn_db": db10(thdn_pw / fund) if fund > 0 else 0.0,
        "thdn_pct": 100.0 * math.sqrt(thdn_pw / fund) if fund > 0 else 0.0,
        "harmonics_db": [db10(hp / fund) if fund > 0 and hp > 0 else -200.0 for hp in harm_pw],
        "noise_floor_dbfs_per_bin": db10(nb / (SINE_RMS ** 2)),
        "peak": float(np.max(np.abs(x))),
        "clip_fraction": float(np.mean(np.abs(x) >= 0.9990)),
    }


def clip_run(x: np.ndarray, level: float = 0.999) -> int:
    """Longest run of consecutive samples at or beyond +-level (hard clipping signature)."""
    m = np.abs(x) >= level
    if not m.any():
        return 0
    d = np.diff(np.concatenate(([0], m.astype(np.int8), [0])))
    s, e = np.where(d == 1)[0], np.where(d == -1)[0]
    return int(np.max(e - s))


def noise_metrics(x: np.ndarray, rate: int, nfft: int = 16384, band=(20.0, 20000.0)) -> dict:
    """Welch noise analysis: unweighted and A-weighted RMS in dBFS (sine-RMS reference), DC, spectrum."""
    x = np.asarray(x, dtype=np.float64)
    dc = float(np.mean(x))
    w = window_bh4(nfft)
    hop = nfft // 2
    acc = np.zeros(nfft // 2 + 1)
    cnt = 0
    for s in range(0, len(x) - nfft + 1, hop):
        seg = x[s:s + nfft]
        sp = np.fft.rfft((seg - np.mean(seg)) * w)
        acc += (np.abs(sp) ** 2) / (nfft * np.sum(w ** 2))
        cnt += 1
    if cnt == 0:
        raise ValueError("recording shorter than one FFT segment")
    p = acc / cnt
    p[1:] *= 2.0
    freqs = np.fft.rfftfreq(nfft, 1.0 / rate)
    m = (freqs >= band[0]) & (freqs <= band[1])
    unw = float(np.sum(p[m]))
    aw = float(np.sum(p[m] * 10.0 ** (a_weight_db(freqs[m]) / 10.0)))
    return {
        "rms_dbfs": db10(unw / SINE_RMS ** 2),
        "rms_a_dbfs": db10(aw / SINE_RMS ** 2),
        "rms_raw_dbfs_peakref": db10(unw),
        "peak_dbfs": db20(np.max(np.abs(x - dc))),
        "dc": dc,
        "dc_dbfs": db20(abs(dc)),
        "freqs": freqs,
        "psd": p,
        "segments": cnt,
    }


def find_spurs(freqs, p, rate_bin_hz: float, n: int = 12, min_db: float = 8.0, lo_hz: float = 20.0):
    """Discrete spurs: bins that exceed the local median (+-60 bins) by min_db.  Returns [(freq, dB over floor, dBFS)]."""
    out = []
    half = 60
    for k in range(max(int(lo_hz / rate_bin_hz), 1), len(p) - 1):
        if p[k] < p[k - 1] or p[k] < p[k + 1]:
            continue
        loc = np.concatenate((p[max(k - half, 0):max(k - 4, 0)], p[k + 5:k + half + 1]))
        if len(loc) < 8:
            continue
        floor = np.median(loc)
        if floor <= 0:
            continue
        over = db10(p[k] / floor)
        if over >= min_db:
            out.append((float(freqs[k]), over, db10(p[k] * 2.0 / SINE_RMS ** 2)))
    out.sort(key=lambda t: -t[1])
    return out[:n]


def sideband_imd(x: np.ndarray, rate: int, f_low: float, f_high: float, orders: int = 4) -> dict:
    """SMPTE-style IMD: sideband power at f_high +- m*f_low (m=1..orders) relative to f_high."""
    freqs, p = power_spectrum(x, rate)
    half = 4.5 * freqs[1]
    carrier = _bin_power(freqs, p, f_high, half)
    sb = sum(_bin_power(freqs, p, f_high + s * m * f_low, half) for m in range(1, orders + 1) for s in (-1, 1))
    return {"imd_db": db10(sb / carrier), "imd_pct": 100 * math.sqrt(sb / carrier)}


def ccif_imd(x: np.ndarray, rate: int, f1: float, f2: float) -> dict:
    """CCIF/ITU-R twin-tone IMD: d2 = A(f2-f1)/(A1+A2), d3 = (A(2f1-f2)+A(2f2-f1))/(A1+A2), in amplitudes."""
    freqs, p = power_spectrum(x, rate)
    half = 4.5 * freqs[1]
    a = lambda f: math.sqrt(_bin_power(freqs, p, f, half)) if 0 < f < rate / 2 else 0.0
    den = a(f1) + a(f2)
    d2 = a(f2 - f1) / den
    d3 = (a(2 * f1 - f2) + a(2 * f2 - f1)) / den
    return {"d2_db": db20(d2), "d3_db": db20(d3), "d2_pct": 100 * d2, "d3_pct": 100 * d3,
            "total_db": db20(math.hypot(d2, d3)), "total_pct": 100 * math.hypot(d2, d3)}


def block_rms(x: np.ndarray, block: int) -> np.ndarray:
    n = len(x) // block
    return np.sqrt(np.mean(x[:n * block].reshape(n, block) ** 2, axis=1))


def matched_delay(template: np.ndarray, recorded: np.ndarray, min_pos: int, max_pos: int) -> tuple[int, float]:
    """Position (frame) of `template` in `recorded` within [min_pos, max_pos] by normalized correlation."""
    m = min(len(recorded), max_pos + len(template))
    n = 1 << int(math.ceil(math.log2(m + len(template))))
    c = np.fft.irfft(np.fft.rfft(recorded[:m], n) * np.conj(np.fft.rfft(template, n)), n)[:max_pos + 1]
    e = np.concatenate(([0.0], np.cumsum(recorded[:m].astype(np.float64) ** 2)))
    seg_e = e[len(template):len(template) + len(c)] - e[:len(c)] if m >= len(template) else np.ones(len(c))
    norm = np.sqrt(np.maximum(seg_e, 1e-300) * float(np.dot(template, template)))
    score = c[:len(norm)] / norm
    lo = max(min_pos, 0)
    k = lo + int(np.argmax(score[lo:]))
    return k, float(score[k])


def interp_crossing(xs, ys, threshold, rising=True):
    """First x where ys crosses threshold (linear interpolation), scanning upward; None if it never does."""
    for i in range(1, len(xs)):
        a, b = ys[i - 1], ys[i]
        if (rising and a < threshold <= b) or (not rising and a > threshold >= b):
            return xs[i - 1] + (xs[i] - xs[i - 1]) * (threshold - a) / (b - a)
    return None


def regress_rate(frames, times):
    """Sample rate (Hz) by least squares of cumulative frames against time, with its standard error."""
    t = np.asarray(times, dtype=np.float64)
    f = np.asarray(frames, dtype=np.float64)
    t0 = t - t.mean()
    slope = float(np.dot(t0, f - f.mean()) / np.dot(t0, t0))
    resid = f - f.mean() - slope * t0
    se = float(np.sqrt(np.sum(resid ** 2) / max(len(t) - 2, 1) / np.dot(t0, t0)))
    return slope, se


def phase_group_delay(played: np.ndarray, recorded: np.ndarray, rate: int, f_lo: float = 20.0, f_hi: float = 20000.0):
    """Transfer function H = R/P of a broadband stimulus: returns freqs, gain dB, unwrapped phase, group delay (s)."""
    n = 1 << int(math.ceil(math.log2(len(played) + len(recorded))))
    P, R = np.fft.rfft(played, n), np.fft.rfft(recorded, n)
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    m = (freqs >= f_lo) & (freqs <= f_hi) & (np.abs(P) > 1e-3 * np.max(np.abs(P)))
    H = np.where(m, R / np.where(P == 0, 1, P), 0)
    fm = freqs[m]
    ph = np.unwrap(np.angle(H[m]))
    gd = -np.gradient(ph, 2 * np.pi * fm)
    return fm, 20 * np.log10(np.abs(H[m]) + 1e-30), ph, gd


def log_sweep(f0: float, f1: float, seconds: float, rate: int, level_dbfs: float, fade: float = 0.05) -> np.ndarray:
    n = int(seconds * rate)
    t = np.arange(n) / rate
    k = math.log(f1 / f0)
    ph = 2 * np.pi * f0 * seconds / k * (np.exp(t * k / seconds) - 1.0)
    x = 10 ** (level_dbfs / 20.0) * np.sin(ph)
    m = int(fade * rate)
    r = 0.5 - 0.5 * np.cos(np.pi * np.arange(m) / m)
    x[:m] *= r
    x[n - m:] *= r[::-1]
    return x
