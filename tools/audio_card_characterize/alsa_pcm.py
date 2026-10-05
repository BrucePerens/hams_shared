# SPDX-License-Identifier: AGPL-3.0-or-later
"""Minimal ALSA full-duplex access through ctypes (libasound), no compiled code and no third-party packages.

One Python process opens playback and capture of the same card, links them so they start on the same
trigger, writes a stereo float stimulus and returns the stereo float capture.  Every capture read is
time-stamped so the real sample rate can be regressed against the system clock.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import time
from dataclasses import dataclass, field

import numpy as np

SND_PCM_STREAM_PLAYBACK = 0
SND_PCM_STREAM_CAPTURE = 1
SND_PCM_ACCESS_RW_INTERLEAVED = 3
SND_PCM_FORMAT_S16_LE = 2
SND_PCM_FORMAT_S24_3LE = 32
FORMATS = {"S16_LE": (SND_PCM_FORMAT_S16_LE, 2), "S24_3LE": (SND_PCM_FORMAT_S24_3LE, 3)}
EPIPE = 32


def _lib():
    name = ctypes.util.find_library("asound") or "libasound.so.2"
    lib = ctypes.CDLL(name)
    lib.snd_pcm_writei.restype = ctypes.c_long
    lib.snd_pcm_readi.restype = ctypes.c_long
    lib.snd_pcm_recover.restype = ctypes.c_int
    lib.snd_strerror.restype = ctypes.c_char_p
    return lib


def _check(lib, rc: int, what: str) -> int:
    if rc < 0:
        raise OSError(rc, f"{what}: {lib.snd_strerror(rc).decode()}")
    return rc


def encode(samples: np.ndarray, fmt: str) -> bytes:
    """Float [-1, 1) frames (n, 2) to interleaved little-endian PCM bytes.  No dither is added."""
    if fmt == "S16_LE":
        return np.clip(np.rint(samples * 32768.0), -32768, 32767).astype("<i2").tobytes()
    v = np.clip(np.rint(samples * 8388608.0), -8388608, 8388607).astype("<i4")
    return v.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()


def decode(raw: bytes, fmt: str, channels: int = 2) -> np.ndarray:
    """Interleaved little-endian PCM bytes to float frames (n, channels)."""
    if fmt == "S16_LE":
        return np.frombuffer(raw, dtype="<i2").astype(np.float64).reshape(-1, channels) / 32768.0
    b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
    v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
    v = (v ^ 0x800000) - 0x800000
    return (v.astype(np.float64) / 8388608.0).reshape(-1, channels)


@dataclass
class DuplexResult:
    capture: np.ndarray                 # (frames, 2) float
    rate: int
    period: int                         # actual period size granted by ALSA
    buffer: int                         # actual buffer size granted by ALSA
    events: dict = field(default_factory=dict)
    xrun_at_frames: list = field(default_factory=list)   # capture frames read when an xrun forced a restart
    xruns_capture: int = 0
    xruns_playback: int = 0
    read_times: list = field(default_factory=list)       # CLOCK_MONOTONIC after each read, seconds
    read_times_raw: list = field(default_factory=list)   # CLOCK_MONOTONIC_RAW
    read_frames: list = field(default_factory=list)      # cumulative capture frames after each read
    write_times: list = field(default_factory=list)      # CLOCK_MONOTONIC after each write returned
    write_frames: list = field(default_factory=list)     # cumulative playback frames accepted
    write_submit: list = field(default_factory=list)     # CLOCK_MONOTONIC just before each write call
    write_delay: list = field(default_factory=list)      # snd_pcm_delay() (frames to the DAC) just before each write


class _Pcm:
    def __init__(self, lib, device: str, stream: int, rate: int, fmt: str, channels: int, period: int, nperiods: int):
        self.lib, self.fmt, self.channels = lib, fmt, channels
        self.handle = ctypes.c_void_p()
        _check(lib, lib.snd_pcm_open(ctypes.byref(self.handle), device.encode(), stream, 0), f"open {device}")
        hw = ctypes.c_void_p()
        _check(lib, lib.snd_pcm_hw_params_malloc(ctypes.byref(hw)), "hw_params_malloc")
        h = self.handle
        _check(lib, lib.snd_pcm_hw_params_any(h, hw), "hw_params_any")
        _check(lib, lib.snd_pcm_hw_params_set_access(h, hw, SND_PCM_ACCESS_RW_INTERLEAVED), "access")
        _check(lib, lib.snd_pcm_hw_params_set_format(h, hw, FORMATS[fmt][0]), f"format {fmt}")
        _check(lib, lib.snd_pcm_hw_params_set_channels(h, hw, channels), "channels")
        _check(lib, lib.snd_pcm_hw_params_set_rate(h, hw, rate, 0), f"rate {rate}")
        p = ctypes.c_ulong(period)
        d = ctypes.c_int(0)
        _check(lib, lib.snd_pcm_hw_params_set_period_size_near(h, hw, ctypes.byref(p), ctypes.byref(d)), "period")
        b = ctypes.c_ulong(period * nperiods)
        _check(lib, lib.snd_pcm_hw_params_set_buffer_size_near(h, hw, ctypes.byref(b)), "buffer")
        _check(lib, lib.snd_pcm_hw_params(h, hw), "hw_params")
        lib.snd_pcm_hw_params_free(hw)
        self.period, self.buffer = int(p.value), int(b.value)
        self.bytes_per_frame = FORMATS[fmt][1] * channels

    def close(self):
        if self.handle:
            self.lib.snd_pcm_close(self.handle)
            self.handle = None


def run_duplex(device: str, stimulus, rate: int = 48000, fmt: str = "S24_3LE",
               period: int = 1024, nperiods: int = 4, tail_frames: int = 0,
               capture_only_frames: int = 0, playback_ahead_periods: int | None = None,
               total_frames: int | None = None, keep: bool = True, on_chunk=None,
               play_window: tuple[int, int] | None = None) -> DuplexResult:
    """Play `stimulus` and record the same number of frames (+ tail_frames), started together.

    `stimulus` is a (frames, 2) float array, or a function (start, count) -> (count, 2) float array
    (then total_frames gives the length), so a 15-minute tone needs no 15-minute buffer.
    capture_only_frames > 0 records that many frames with the playback stream left closed.
    keep=False discards the capture and hands each chunk to on_chunk(float array, cumulative frames) instead.
    play_window=(start, stop): with capture_only_frames, open playback once `start` capture frames have been read
    (stimulus index 0 is the first played frame) and drain+close it at `stop`; events holds the capture frame
    index and CLOCK_MONOTONIC of each, for click-and-pop analysis.
    """
    lib = _lib()
    play = cap = None
    try:
        cap = _Pcm(lib, device, SND_PCM_STREAM_CAPTURE, rate, fmt, 2, period, nperiods)
        if callable(stimulus):
            source, nplay = stimulus, int(total_frames)
        else:
            arr = np.zeros((0, 2)) if stimulus is None else np.asarray(stimulus, dtype=np.float64)
            nplay = len(arr)
            source = lambda start, count: arr[start:start + count]
        total = capture_only_frames or (nplay + tail_frames)
        if not capture_only_frames:
            play = _Pcm(lib, device, SND_PCM_STREAM_PLAYBACK, rate, fmt, 2, period, nperiods)
            _check(lib, lib.snd_pcm_link(play.handle, cap.handle), "link")
        _check(lib, lib.snd_pcm_prepare(cap.handle), "prepare capture")
        if play:
            _check(lib, lib.snd_pcm_prepare(play.handle), "prepare playback")
        period_g = cap.period
        res = DuplexResult(np.zeros((0, 2)), rate, period_g, cap.buffer)
        out_chunks = []
        pad = source
        wpos = 0
        rbuf = ctypes.create_string_buffer(period_g * cap.bytes_per_frame)
        ahead = playback_ahead_periods if playback_ahead_periods is not None else nperiods
        if play:   # prefill the playback queue; the first write triggers both linked streams
            for _ in range(ahead):
                wpos = _write(lib, play, res, pad, wpos, period_g)
        else:
            _check(lib, lib.snd_pcm_start(cap.handle), "start capture")
        got = 0
        errors = 0
        deadline = time.monotonic() + total / rate * 1.5 + 15.0   # a stalled stream must fail, never spin
        window_state = 0   # 0 not opened yet, 1 playing, 2 closed
        while got < total:
            if play_window and window_state == 0 and got >= play_window[0]:
                play = _Pcm(lib, device, SND_PCM_STREAM_PLAYBACK, rate, fmt, 2, period, nperiods)
                _check(lib, lib.snd_pcm_prepare(play.handle), "prepare playback")
                wpos = 0
                for _ in range(ahead):
                    wpos = _write(lib, play, res, pad, wpos, period_g)
                res.events["play_start"] = (got, time.clock_gettime(time.CLOCK_MONOTONIC))
                window_state = 1
            elif play_window and window_state == 1 and got >= play_window[1]:
                res.events["play_stop_request"] = (got, time.clock_gettime(time.CLOCK_MONOTONIC))
                lib.snd_pcm_drain(play.handle)
                res.events["play_drained"] = (got, time.clock_gettime(time.CLOCK_MONOTONIC))
                play.close()
                play = None
                window_state = 2
            if time.monotonic() > deadline:
                raise TimeoutError(f"capture stalled after {got} of {total} frames")
            n = lib.snd_pcm_readi(cap.handle, rbuf, period_g)
            if n < 0:
                errors += 1
                if errors > 50:
                    raise OSError(int(n), f"capture read keeps failing: {lib.snd_strerror(int(n)).decode()}")
                if -n == EPIPE:
                    res.xruns_capture += 1
                # an xrun on either linked stream stops both: re-prepare, refill the playback queue, restart
                res.xrun_at_frames.append(got)
                lib.snd_pcm_drop(cap.handle)
                if play:
                    lib.snd_pcm_drop(play.handle)
                lib.snd_pcm_prepare(cap.handle)
                if play:
                    lib.snd_pcm_prepare(play.handle)
                    for _ in range(ahead):
                        wpos = _write(lib, play, res, pad, wpos, period_g)
                else:
                    lib.snd_pcm_start(cap.handle)
                continue
            errors = 0
            now, raw = time.clock_gettime(time.CLOCK_MONOTONIC), time.clock_gettime(time.CLOCK_MONOTONIC_RAW)
            got += n
            res.read_times.append(now)
            res.read_times_raw.append(raw)
            res.read_frames.append(got)
            if keep or on_chunk:
                frames = decode(rbuf.raw[:n * cap.bytes_per_frame], fmt)
                if keep:
                    out_chunks.append(frames)
                if on_chunk:
                    on_chunk(frames, got)
            if play:
                wpos = _write(lib, play, res, pad, wpos, period_g)
        if keep:
            res.capture = np.concatenate(out_chunks)[:total]
        return res
    finally:
        for p in (play, cap):
            if p:
                try:
                    if p.handle:
                        lib.snd_pcm_drop(p.handle)
                except Exception:
                    pass
        for p in (play, cap):
            if p:
                p.close()


def _write(lib, play: _Pcm, res: DuplexResult, pad: np.ndarray, wpos: int, period: int) -> int:
    block = pad(wpos, period)
    if len(block) < period:
        block = np.concatenate((block, np.zeros((period - len(block), 2))))
    chunk = encode(block, play.fmt)
    buf = ctypes.create_string_buffer(chunk, len(chunk))
    dly = ctypes.c_long(0)
    res.write_delay.append(int(dly.value) if lib.snd_pcm_delay(play.handle, ctypes.byref(dly)) == 0 else -1)
    res.write_submit.append(time.clock_gettime(time.CLOCK_MONOTONIC))
    n = lib.snd_pcm_writei(play.handle, buf, period)
    if n < 0:
        if -n == EPIPE:
            res.xruns_playback += 1
        lib.snd_pcm_recover(play.handle, int(n), 1)
        n = 0
    res.write_times.append(time.clock_gettime(time.CLOCK_MONOTONIC))
    res.write_frames.append(wpos + int(n))
    return wpos + int(n)
