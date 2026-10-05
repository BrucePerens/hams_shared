#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hold the freedesktop.org ReserveDevice1 reservation for an ALSA card so PipeWire/WirePlumber/PulseAudio
let go of it, without stopping the sound server.  Run it in the background, measure, then send SIGTERM.

  python3 reserve_device.py --card 1 &      # prints RESERVED when the card is yours
  kill <pid>                                # releases; the sound server takes the card back
Needs python3-gi (Gio).  Linux only.
"""
from __future__ import annotations

import argparse
import signal
import sys

PRIORITY = 10          # WirePlumber announces -20, PulseAudio 5, JACK 0
XML = """<node><interface name='org.freedesktop.ReserveDevice1'>
<method name='RequestRelease'><arg type='i' name='priority' direction='in'/><arg type='b' name='result' direction='out'/></method>
<property name='Priority' type='i' access='read'/><property name='ApplicationName' type='s' access='read'/>
<property name='ApplicationDeviceName' type='s' access='read'/></interface></node>"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--card", type=int, required=True)
    ap.add_argument("--timeout", type=float, default=10.0)
    a = ap.parse_args()
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
    name = f"org.freedesktop.ReserveDevice1.Audio{a.card}"
    path = f"/org/freedesktop/ReserveDevice1/Audio{a.card}"
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    loop = GLib.MainLoop()
    state = {"owned": False}

    def method(conn, sender, obj, iface, meth, params, inv):
        inv.return_value(GLib.Variant("(b)", (False,)))   # we never give the card back to a request

    def prop(conn, sender, obj, iface, p):
        return {"Priority": GLib.Variant("i", PRIORITY), "ApplicationName": GLib.Variant("s", "audio_card_characterize"),
                "ApplicationDeviceName": GLib.Variant("s", f"hw:{a.card}")}[p]

    info = Gio.DBusNodeInfo.new_for_xml(XML)
    bus.register_object(path, info.interfaces[0], method, prop, None)
    try:    # ask the current holder to release first (WirePlumber answers true)
        bus.call_sync(name, path, "org.freedesktop.ReserveDevice1", "RequestRelease",
                      GLib.Variant("(i)", (PRIORITY,)), None, Gio.DBusCallFlags.NONE, 3000, None)
    except GLib.Error as e:
        print(f"RequestRelease: {e.message}", file=sys.stderr)

    def acquired(c, n):
        state["owned"] = True
        print("RESERVED", name, flush=True)

    def lost(c, n):
        if not state["owned"]:
            print("FAILED to own", name, flush=True)
            loop.quit()

    flags = Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT | Gio.BusNameOwnerFlags.REPLACE | Gio.BusNameOwnerFlags.DO_NOT_QUEUE
    Gio.bus_own_name_on_connection(bus, name, flags, acquired, lost)
    GLib.timeout_add_seconds(int(a.timeout), lambda: (loop.quit() if not state["owned"] else None) or False)
    for s in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, s, lambda: loop.quit() or True)
    loop.run()
    print("released", flush=True)
    return 0 if state["owned"] else 1


if __name__ == "__main__":
    sys.exit(main())
