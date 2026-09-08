---
name: gate-continuous-dsp-work
description: "Bruce wants continuously-running DSP/recognizer work in the hams relay gated to when it is actually needed, not run unconditionally."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: c0284e30-6ccb-4ad4-9b91-456b9fb13774
  modified: 2026-09-08T18:49:48.834Z
---

# Gate continuously-running DSP work to when it is actually needed

Bruce (K6BP), working on the `hams_com` / `hams_open` codebase, holds a standing
position that background signal-processing work must not run unconditionally: it
should be gated behind a real signal that its output is currently wanted. He has
directed this repeatedly rather than as a one-off, so treat it as the default
design stance whenever adding or reviewing anything that runs per audio chunk or
on a periodic timer in `daemons/hams_local_relay`.

Concrete instances of the same instruction:

- The whole per-chunk decode cascade in `digital_decoder.rs` used to run against
  whatever the system's default microphone picked up even with zero radios
  configured, costing about 30% of a CPU continuously. It is now gated behind
  `radio_connected_rx`.
- `cpu_governor.rs` exists because Bruce asked specifically for a swamped CPU to
  be detected and RADE (the expensive neural vocoder) disabled in response, with
  hysteresis and auto-recovery rather than a permanent shutoff.
- On 2026-09-08 he extended the same principle further: even with a radio
  connected, once the operating mode is known or the user has selected it, the
  mode *recognizers* should not be processing until the operator actually presses
  auto-tune, auto-mode, or a frequency up/down button.

The useful engineering distinction when applying this, which is worth stating
back to him rather than assuming: a *recognizer* (the analog mode classifier, an
auto-tune frequency search bank) exists only to answer a question someone asked,
so it is safe to gate on demand. A *decoder* whose output the operator is
actually watching (the FT8/WSPR/PSK31 decode feed) is the product feature itself,
so gating it removes function rather than waste. Cheap buffer accumulation can
usually keep running so that an on-demand answer has no added latency, while the
expensive transform it feeds runs only when asked.
