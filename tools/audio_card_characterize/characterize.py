#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Characterize a sound card whose output is looped back to its input.  See README.md.

Example (card 1, ALSA name S3), every run restores the mixer on exit:
  python3 characterize.py --card S3 --out out inspect
  python3 characterize.py --card S3 --out out channels
  python3 characterize.py --card S3 --out out levels --play-db -24 --cap-db 0.5 --freq 1000
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import measure  # noqa: E402
from mixer import Mixer  # noqa: E402


def to_jsonable(o):
    if isinstance(o, dict):
        return {str(k): to_jsonable(v) for k, v in o.items() if not str(k).startswith("_")}
    if isinstance(o, (list, tuple)):
        return [to_jsonable(v) for v in o]
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, float) and (o != o or o in (float("inf"), float("-inf"))):
        return None
    return o


def save(out_dir: str, name: str, obj) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name)
    with open(path, "w") as fh:
        json.dump(to_jsonable(obj), fh, indent=1)
    print("wrote", path, flush=True)
    return path


def build_rig(a) -> measure.Rig:
    mixer = Mixer(a.mixer_card or a.card_index)
    backend = measure.AlsaBackend(f"hw:CARD={a.card},DEV=0", rate=a.rate, fmt=a.format)
    return measure.Rig(backend, mixer, play_ctl=a.play_ctl, cap_ctl=a.cap_ctl, switch_ctl=a.switch_ctl,
                       rate=a.rate, play_channels=[int(c) for c in a.play_channels.split(",")],
                       cap_channel=a.cap_channel, period=a.period, nperiods=a.nperiods)


def apply_settings(rig, a):
    if getattr(a, "play_db", None) is not None:
        print("playback volume ->", rig.set_play_db(a.play_db), "dB", flush=True)
    if getattr(a, "cap_db", None) is not None:
        print("capture gain ->", rig.set_cap_db(a.cap_db), "dB", flush=True)


def cmd_inspect(rig, a):
    ctl = rig.mixer.controls()
    info = {n: {"numid": c.numid, "type": c.ctype, "min": c.vmin, "max": c.vmax, "db_min": c.db_min, "db_max": c.db_max,
                "values": c.values} for n, c in ctl.items()}
    stream = ""
    p = f"/proc/asound/card{a.card_index}/stream0"
    if os.path.exists(p):
        stream = open(p).read()
    save(a.out, "inspect.json", {"controls": info, "stream0": stream, "contents": rig.mixer.contents()})
    print(rig.mixer.contents())
    print(stream)


def cmd_channels(rig, a):
    apply_settings(rig, a)
    save(a.out, "channels.json", measure.channels_test(rig, a.level))


def cmd_levels(rig, a):
    apply_settings(rig, a)
    for f in [float(v) for v in a.freq.split(",")]:
        r = measure.levels_sweep(rig, f, lo=a.lo, hi=a.hi, step=a.step, seg_s=a.seg)
        r["summary"] = measure.headroom_summary(r["rows"])
        s = r["settings"]
        save(a.out, f"levels_f{int(f)}_p{s['play_raw']}_c{s['cap_raw']}.json", r)
        print(json.dumps(to_jsonable(r["summary"])), flush=True)


def cmd_noise(rig, a):
    apply_settings(rig, a)
    res = {}
    for state in a.states.split(","):
        m = measure.noise_run(rig, a.seconds, state)
        res[state] = m
        print(state, "rms %.1f  A %.1f dBFS" % (m["rms_dbfs"], m["rms_a_dbfs"]), flush=True)
    s = rig.settings()
    save(a.out, f"noise_p{s['play_raw']}_c{s['cap_raw']}.json", res)
    for state, m in res.items():       # PSD csv at the finest resolution for the plots
        step = max(1, len(m["_freqs"]) // 4096)
        with open(os.path.join(a.out, f"psd_{state}_c{s['cap_raw']}.csv"), "w") as fh:
            fh.write("freq_hz,dbfs_per_bin\n")
            for f, p in zip(m["_freqs"][::step], m["_psd"][::step]):
                fh.write(f"{f:.2f},{10 * np.log10(max(p, 1e-30) / 0.5):.2f}\n")


def cmd_thdfreq(rig, a):
    apply_settings(rig, a)
    save(a.out, "thd_vs_freq.json", measure.thd_vs_freq(rig, measure.thirds(20, 20000, 3), [float(v) for v in a.targets.split(",")]))


def cmd_response(rig, a):
    apply_settings(rig, a)
    r = measure.frequency_response(rig, measure.thirds(20, 20000, 6))
    r["sweep"] = measure.sweep_phase(rig)
    save(a.out, "frequency_response.json", r)
    print(json.dumps(to_jsonable(r.get("analysis"))))


def cmd_imd(rig, a):
    apply_settings(rig, a)
    save(a.out, "imd.json", measure.imd_run(rig))


def cmd_longrun(rig, a):
    apply_settings(rig, a)
    r = measure.long_run(rig, a.seconds, a.level, period=a.period, nperiods=a.nperiods)
    save(a.out, f"longrun_{int(a.seconds)}s.json", r)


def cmd_xruns(rig, a):
    apply_settings(rig, a)
    res = [measure.xrun_run(rig, int(p), a.nperiods, a.seconds) for p in a.periods.split(",")]
    save(a.out, "xruns.json", res)


def cmd_latency(rig, a):
    apply_settings(rig, a)
    g = rig.probe_gain()
    lvl = -20.0 if g is None else min(-6.0, -20.0 - g)
    print("probe gain", g, "burst level", lvl, flush=True)
    res = []
    for p, n, ahead in [(64, 3, 2), (128, 3, 2), (256, 3, 2), (512, 3, 2), (1024, 3, 2), (64, 4, 4), (256, 4, 4), (1024, 4, 4)]:
        r = measure.latency_run(rig, p, n, ahead, lvl)
        res.append(r)
        print(json.dumps(to_jsonable(r)), flush=True)
    save(a.out, "latency.json", res)


def cmd_playsweep(rig, a):
    apply_settings(rig, a)
    save(a.out, "play_volume_sweep.json", measure.play_volume_sweep(rig, a.level))


def cmd_capsweep(rig, a):
    apply_settings(rig, a)
    save(a.out, "cap_gain_sweep.json", measure.cap_gain_sweep(rig, a.level))


def cmd_dynamics(rig, a):
    apply_settings(rig, a)
    s = rig.settings()
    save(a.out, f"dynamics_p{s['play_raw']}_c{s['cap_raw']}_{int(a.low)}_{int(a.high)}.json",
         measure.limiter_dynamics(rig, a.low, a.high))


def cmd_clicks(rig, a):
    apply_settings(rig, a)
    ctl = rig.mixer.controls()
    pc, cc = ctl[rig.play_ctl], ctl[rig.cap_ctl]
    p0, c0 = int(pc.values[0]), int(cc.values[0])
    res = {}
    res["start_stop_after_idle"] = measure.click_run(rig, 42.0, [], play_window=(30.0, 36.0), play_dbfs=None)
    res["start_stop_quick"] = measure.click_run(rig, 14.0, [], play_window=(3.0, 6.0), play_dbfs=None)
    plan = [(2.0, "play_vol_+1", lambda: rig.set_play_raw(min(p0 + 1, pc.vmax))),
            (4.0, "play_vol_-1", lambda: rig.set_play_raw(p0)),
            (6.0, "play_vol_+10", lambda: rig.set_play_raw(min(p0 + 10, pc.vmax))),
            (8.0, "play_vol_-10", lambda: rig.set_play_raw(p0)),
            (10.0, "play_switch_off", lambda: rig.mixer.set(rig.switch_ctl, "off")),
            (12.0, "play_switch_on", lambda: rig.mixer.set(rig.switch_ctl, "on")),
            (14.0, "cap_gain_+1", lambda: rig.set_cap_raw(min(c0 + 1, cc.vmax))),
            (16.0, "cap_gain_-1", lambda: rig.set_cap_raw(c0)),
            (18.0, "cap_gain_+10", lambda: rig.set_cap_raw(min(c0 + 10, cc.vmax))),
            (20.0, "cap_gain_-10", lambda: rig.set_cap_raw(c0))]
    # silence is played as a zero-amplitude tone through the same code path
    res["mixer_events_silence"] = measure.click_run(rig, 23.0, plan, play_dbfs=-200.0)
    rig.set_play_raw(p0)
    rig.set_cap_raw(c0)
    res["mixer_events_tone"] = measure.click_run(rig, 23.0, plan, play_dbfs=a.level)
    save(a.out, "clicks.json", res)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--card", required=True, help="ALSA card id, e.g. S3 (uses hw:CARD=<id>,DEV=0)")
    ap.add_argument("--card-index", default="1", help="card number for amixer / /proc/asound (default 1)")
    ap.add_argument("--mixer-card", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--rate", type=int, default=48000)
    ap.add_argument("--format", default="S24_3LE", choices=["S16_LE", "S24_3LE"])
    ap.add_argument("--play-ctl", default="Speaker Playback Volume")
    ap.add_argument("--cap-ctl", default="Mic Capture Volume")
    ap.add_argument("--switch-ctl", default="Speaker Playback Switch")
    ap.add_argument("--play-channels", default="0", help="comma list of playback channels that reach the input")
    ap.add_argument("--cap-channel", type=int, default=0)
    ap.add_argument("--period", type=int, default=1024)
    ap.add_argument("--nperiods", type=int, default=4)
    ap.add_argument("--keep-mixer", action="store_true", help="do not restore the mixer on exit")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **extra):
        p = sub.add_parser(name)
        p.add_argument("--play-db", type=float, default=None, help="playback mixer gain in dB (the control's own scale)")
        p.add_argument("--cap-db", type=float, default=None, help="capture mixer gain in dB")
        for k, v in extra.items():
            p.add_argument("--" + k.replace("_", "-"), **v)
        p.set_defaults(fn=fn)
        return p

    add("inspect", cmd_inspect)
    add("channels", cmd_channels, level=dict(type=float, default=-20.0))
    add("levels", cmd_levels, freq=dict(default="1000"), lo=dict(type=float, default=-90.0),
        hi=dict(type=float, default=-6.0), step=dict(type=float, default=1.0), seg=dict(type=float, default=0.8))
    add("noise", cmd_noise, seconds=dict(type=float, default=20.0), states=dict(default="stopped,silence,muted"))
    add("thdfreq", cmd_thdfreq, targets=dict(default="-3,-10,-20"))
    add("response", cmd_response)
    add("imd", cmd_imd)
    add("longrun", cmd_longrun, seconds=dict(type=float, default=900.0), level=dict(type=float, default=-20.0))
    add("xruns", cmd_xruns, seconds=dict(type=float, default=300.0), periods=dict(default="64,128,256"))
    add("latency", cmd_latency)
    add("playsweep", cmd_playsweep, level=dict(type=float, default=-40.0))
    add("capsweep", cmd_capsweep, level=dict(type=float, default=-50.0))
    add("clicks", cmd_clicks, level=dict(type=float, default=-30.0))
    add("dynamics", cmd_dynamics, low=dict(type=float, default=-40.0), high=dict(type=float, default=-10.0))
    a = ap.parse_args(argv)
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))   # let the finally block restore the mixer
    rig = build_rig(a)
    snap = rig.mixer.snapshot()
    os.makedirs(a.out, exist_ok=True)
    save(a.out, "mixer_before_%s.json" % a.cmd, {str(k): v for k, v in snap.items()})
    try:
        a.fn(rig, a)
    finally:
        if not a.keep_mixer:
            rig.mixer.restore(snap)
            print("mixer restored", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
