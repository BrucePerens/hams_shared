# SPDX-License-Identifier: AGPL-3.0-or-later
#!/usr/bin/env python3
"""Plot the JSON/CSV results written by characterize.py into PNG files.  usage: make_plots.py <data dir> <out dir>

matplotlib only; every plot is skipped (with a message) when its data file is missing.
"""
from __future__ import annotations

import glob
import json
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

PALETTE = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#000000", "#F0E442"]


def load(path):
    with open(path) as fh:
        return json.load(fh)


def style(ax, xlabel, ylabel, title):
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title, fontsize=10)
    ax.grid(True, color="#dddddd", linewidth=0.6)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


def save(fig, out, name):
    fig.tight_layout()
    fig.savefig(os.path.join(out, name), dpi=130)
    plt.close(fig)
    print("wrote", name)


def plot_levels(data, out):
    files = sorted(glob.glob(os.path.join(data, "levels_f1000_p*_c*.json")))
    if not files:
        print("no levels data")
        return
    cells = [load(f) for f in files]
    caps = sorted({c["settings"]["cap_db"] for c in cells})
    fig, axes = plt.subplots(2, 2, figsize=(12, 9), sharex=True)
    for ax, cap in zip(axes.flat, caps):
        sel = [c for c in cells if c["settings"]["cap_db"] == cap]
        for i, c in enumerate(sorted(sel, key=lambda c: c["settings"]["play_db"])):
            rows = c["rows"]
            ax.plot([r["play_dbfs"] for r in rows], [r["level_dbfs"] for r in rows], color=PALETTE[i % 8], linewidth=1.2,
                    label=f"playback volume {c['settings']['play_db']:.1f} dB")
        ax.plot([-90, 0], [-90, 0], color="#999999", linewidth=0.6, linestyle=":")
        style(ax, "digital playback level (dBFS)", "capture level (dBFS)", f"capture gain {cap:.1f} dB")
        ax.legend(fontsize=7, loc="upper left")
    save(fig, out, "levels_capture_vs_playback.png")
    fig, axes = plt.subplots(2, 2, figsize=(12, 9), sharex=True)
    for ax, cap in zip(axes.flat, caps):
        sel = [c for c in cells if c["settings"]["cap_db"] == cap]
        for i, c in enumerate(sorted(sel, key=lambda c: c["settings"]["play_db"])):
            rows = [r for r in c["rows"] if -r["thdn_db"] > 20]
            ax.semilogy([r["play_dbfs"] for r in rows], [max(r["thd_pct"], 1e-4) for r in rows], color=PALETTE[i % 8],
                        linewidth=1.0, label=f"playback volume {c['settings']['play_db']:.1f} dB")
        for thr in (0.1, 1, 3):
            ax.axhline(thr, color="#bbbbbb", linewidth=0.6)
        style(ax, "digital playback level (dBFS)", "harmonic THD (%)  [floor 1e-4]", f"capture gain {cap:.1f} dB")
        ax.legend(fontsize=7)
    save(fig, out, "levels_thd.png")


def plot_noise(data, out):
    fig, ax = plt.subplots(figsize=(8, 5))
    n = 0
    for f in sorted(glob.glob(os.path.join(data, "psd_*_c*.csv"))):
        xs, ys = [], []
        for line in open(f).read().splitlines()[1:]:
            a, b = line.split(",")
            if float(a) > 10:
                xs.append(float(a))
                ys.append(float(b))
        ax.semilogx(xs, ys, linewidth=0.7, color=PALETTE[n % 8], label=os.path.basename(f)[4:-4])
        n += 1
    if n:
        style(ax, "frequency (Hz)", "dBFS per bin (2.9 Hz)", "idle capture noise spectrum")
        ax.legend(fontsize=7)
        save(fig, out, "noise_spectra.png")


def plot_response(data, out):
    p = os.path.join(data, "frequency_response.json")
    if not os.path.exists(p):
        return
    d = load(p)
    fig, axes = plt.subplots(2, 1, figsize=(8, 7), sharex=True)
    rows = d["rows"]
    axes[0].semilogx([r["freq_nominal"] for r in rows], [r["response_db"] for r in rows], marker="o", markersize=3,
                     color=PALETTE[0], label="stepped tones")
    sw = d.get("sweep") or {}
    if sw.get("freq_hz"):
        ref = min(range(len(sw["freq_hz"])), key=lambda i: abs(sw["freq_hz"][i] - 1000))
        axes[0].semilogx(sw["freq_hz"], [g - sw["gain_db"][ref] for g in sw["gain_db"]], color=PALETTE[1], linewidth=1,
                         label="log sweep")
        axes[1].semilogx(sw["freq_hz"], sw["group_delay_ms"], color=PALETTE[2])
    axes[0].axhline(-3, color="#bbbbbb", linewidth=0.6)
    axes[0].set_ylim(-20, 5)
    style(axes[0], "", "response re 1 kHz (dB)", "frequency response")
    axes[0].legend(fontsize=8)
    style(axes[1], "frequency (Hz)", "group delay (ms)", "group delay (sweep deconvolution)")
    save(fig, out, "frequency_response.png")


def plot_thdfreq(data, out):
    p = os.path.join(data, "thd_vs_freq.json")
    if not os.path.exists(p):
        return
    d = load(p)
    fig, ax = plt.subplots(figsize=(8, 5))
    for i, (t, v) in enumerate(d["targets"].items()):
        if v["rows"]:
            ax.semilogx([r["freq_nominal"] for r in v["rows"]], [r["thdn_db"] for r in v["rows"]], marker="o", markersize=3,
                        color=PALETTE[i % 8], label=f"capture {t} dBFS")
    style(ax, "frequency (Hz)", "THD+N (dB re signal)", "THD+N versus frequency (20 Hz - 20 kHz band)")
    ax.legend(fontsize=8)
    save(fig, out, "thdn_vs_frequency.png")


def plot_dynamics(data, out):
    files = sorted(glob.glob(os.path.join(data, "dynamics_*.json")))
    if not files:
        return
    fig, ax = plt.subplots(figsize=(9, 5))
    for i, f in enumerate(files):
        d = load(f)
        if "env_dbfs" in d:
            ax.plot(d["t_s"], d["env_dbfs"], linewidth=0.9, color=PALETTE[i % 8], label=f"capture gain {d['settings']['cap_db']:.1f} dB")
    ax.set_xlim(-0.5, 11)
    ax.set_ylim(-70, 0)
    style(ax, "time from tone onset (s)", "capture envelope (dBFS)", "limiter: tone -40 dBFS, up to -10 dBFS at 2 s, back at 5 s")
    ax.legend(fontsize=8)
    save(fig, out, "limiter_dynamics.png")


def plot_sweeps(data, out):
    p = os.path.join(data, "cap_gain_sweep.json")
    if os.path.exists(p):
        d = load(p)
        fig, ax = plt.subplots(figsize=(8, 5))
        r = d["rows"]
        ax.plot([x["nominal_db"] for x in r], [x["level_dbfs"] for x in r], color=PALETTE[0], label="tone level")
        ax.plot([x["nominal_db"] for x in r], [x["noise_rms_dbfs"] for x in r], color=PALETTE[1], label="idle noise (rms)")
        style(ax, "capture control (nominal dB)", "dBFS", f"capture gain sweep, tone at {d['play_dbfs']} dBFS")
        ax.legend()
        save(fig, out, "capture_gain_sweep.png")
    p = os.path.join(data, "play_volume_sweep.json")
    if os.path.exists(p):
        d = load(p)
        r = d["rows"]
        fig, ax = plt.subplots(figsize=(8, 5))
        ax.plot([x["nominal_db"] for x in r], [x["level_dbfs"] for x in r], color=PALETTE[0])
        style(ax, "playback control (nominal dB)", "capture level (dBFS)", f"playback volume sweep, tone at {d['play_dbfs']} dBFS")
        save(fig, out, "playback_volume_sweep.png")


def plot_longrun(data, out):
    for p in glob.glob(os.path.join(data, "longrun_*s.json")):
        d = load(p)
        w = d["windows"]
        fig, axes = plt.subplots(3, 1, figsize=(8, 7), sharex=True)
        t = [x["t_s"] / 60 for x in w]
        axes[0].plot(t, [x["level_dbfs"] for x in w], color=PALETTE[0])
        axes[1].plot(t, [(x["freq_hz"] / d["freq_hz"] - 1) * 1e6 for x in w], color=PALETTE[1])
        axes[2].plot(t, [x["thdn_db"] for x in w], color=PALETTE[2])
        style(axes[0], "", "level (dBFS)", "long run: level")
        style(axes[1], "", "tone ppm offset", "captured tone frequency vs 1000 Hz (playback/capture clock ratio)")
        style(axes[2], "time (min)", "THD+N (dB)", "THD+N")
        save(fig, out, "longrun_stability.png")


def plot_latency(data, out):
    p = os.path.join(data, "latency.json")
    if not os.path.exists(p):
        return
    d = load(p)
    fig, ax = plt.subplots(figsize=(8, 5))
    lab = [f"{r['period']}x{r['buffer'] // r['period']} ahead {r['ahead']}" for r in d]
    ax.bar(range(len(d)), [r["app_ms"]["mean"] for r in d], color=PALETTE[0], label="app write-to-read")
    ax.bar([i + 0.0 for i in range(len(d))], [r["stream_ms"]["mean"] for r in d], width=0.4, color=PALETTE[1], label="stream offset")
    ax.set_xticks(range(len(d)))
    ax.set_xticklabels(lab, rotation=30, fontsize=7)
    style(ax, "period x periods", "ms", "loopback latency")
    ax.legend()
    save(fig, out, "latency.png")


def main() -> int:
    data, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    for fn in (plot_levels, plot_noise, plot_response, plot_thdfreq, plot_dynamics, plot_sweeps, plot_longrun, plot_latency):
        try:
            fn(data, out)
        except Exception as e:  # a missing or partial data file must not stop the other plots
            print(fn.__name__, "skipped:", repr(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
