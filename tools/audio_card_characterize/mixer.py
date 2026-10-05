# SPDX-License-Identifier: AGPL-3.0-or-later
"""ALSA mixer access through the amixer command (read, set, snapshot, restore)."""
from __future__ import annotations

import re
import subprocess


def _run(args: list[str]) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


class Control:
    def __init__(self, numid: int, name: str, ctype: str, vmin: int | None, vmax: int | None,
                 db_min: float | None, db_max: float | None, values: list[str]):
        self.numid, self.name, self.ctype = numid, name, ctype
        self.vmin, self.vmax, self.db_min, self.db_max, self.values = vmin, vmax, db_min, db_max, values

    def db_at(self, raw: int) -> float | None:
        """dB of a raw value, assuming the usual linear-in-dB TLV scale (verify by measurement)."""
        if self.db_min is None or self.vmax == self.vmin:
            return None
        return self.db_min + (self.db_max - self.db_min) * (raw - self.vmin) / (self.vmax - self.vmin)

    def raw_for_db(self, db: float) -> int:
        frac = (db - self.db_min) / (self.db_max - self.db_min)
        return int(round(self.vmin + frac * (self.vmax - self.vmin)))


class Mixer:
    def __init__(self, card: str):
        self.card = card

    def contents(self) -> str:
        return _run(["amixer", "-c", self.card, "contents"])

    def controls(self) -> dict[str, Control]:
        out: dict[str, Control] = {}
        blocks = re.split(r"(?m)^(?=numid=)", self.contents())
        for b in blocks:
            m = re.match(r"numid=(\d+),iface=\w+,name='([^']*)'", b)
            if not m:
                continue
            t = re.search(r"type=(\w+)", b)
            r = re.search(r"min=(-?\d+),max=(-?\d+)", b)
            d = re.search(r"dBminmax-min=(-?[\d.]+)dB,max=(-?[\d.]+)dB", b)
            v = re.search(r": values=(.*)", b)
            out[m.group(2)] = Control(int(m.group(1)), m.group(2), t.group(1) if t else "?",
                                      int(r.group(1)) if r else None, int(r.group(2)) if r else None,
                                      float(d.group(1)) if d else None, float(d.group(2)) if d else None,
                                      v.group(1).split(",") if v else [])
        return out

    def set(self, name_or_numid, value) -> None:
        numid = name_or_numid if isinstance(name_or_numid, int) else self.controls()[name_or_numid].numid
        val = ",".join(str(v) for v in value) if isinstance(value, (list, tuple)) else str(value)
        _run(["amixer", "-c", self.card, "-q", "cset", f"numid={numid}", val])

    def get(self, name: str) -> list[str]:
        return self.controls()[name].values

    def snapshot(self) -> dict[int, str]:
        """numid -> value string for every writable control."""
        snap = {}
        for c in self.controls().values():
            if c.values and c.ctype in ("BOOLEAN", "INTEGER", "ENUMERATED"):
                snap[c.numid] = ",".join(c.values)
        return snap

    def restore(self, snap: dict[int, str]) -> None:
        for numid, val in snap.items():
            try:
                self.set(numid, val)
            except subprocess.CalledProcessError:
                pass   # read-only control (channel maps)
