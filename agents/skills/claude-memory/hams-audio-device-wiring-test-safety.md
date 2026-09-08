---
name: hams-audio-device-wiring-test-safety
description: "Before wiring real ALSA/audio-device detection into any hams.com/hams_open code path exercised by cargo test, check whether it will make tests emit real, audible sound or grab real hardware."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 44fa3b1d-b189-4e0a-bf9c-d06127ace94d
  modified: 2026-09-03T20:38:48.450Z
---

On hams.com/hams_open's `hams_local_relay` daemon, `winlink.rs` already had a real ALSA-detection
helper, `detect_radio_audio_device()`, wired into the Direwolf (AX.25) spawn path so it picks up a
real `plughw:CARD,DEVICE` string instead of `"null"`. `ardop.rs`'s `spawn_ardopcf` had a matching,
explicitly-documented TODO: its own doc comment said real device routing was "not yet wired here,"
with `"null"`/`"null"` hardcoded at its one call site in `winlink.rs`'s `handle_winlink_command`.

During a 2026-09-03 session, Claude wired `detect_radio_audio_device()` into that `ardopcf` call site
too, mirroring the Direwolf pattern, and immediately ran `cargo test --release ardop` to verify it.
This was a real mistake with real physical consequences, not a hypothetical risk: that exact code
path is directly exercised by a real integration test,
`handle_winlink_command_spawns_ardopcf_and_runs_a_real_pat_connect_for_an_ardop_url`, which spawns a
real `ardopcf` process. With a real ALSA device wired in instead of `"null"`, running the test suite
made `ardopcf` open the dev box's actual laptop speaker and microphone and play a real, audible ARDOP
connection handshake through them -- Bruce heard it live, described it as sounding like "acquisition
tones for ALE," and it was still audible/the ALSA device still not fully released for several tool
calls after the test process itself had exited. Confirmed via `wpctl status`: PipeWire's own sink
list dropped to just a synthetic "Dummy Output" for a while afterward, because `ardopcf`'s raw
`plughw:` access bypassed PipeWire and grabbed the real hardware device exclusively. The fix was an
immediate revert back to hardcoded `"null"`/`"null"` at that call site, restoring the previously
already-verified-silent behavior.

The generalized lesson for this codebase, since it is fundamentally about real radio hardware and
real audio I/O: any code path that opens a real ALSA/audio device, keys real PTT, or otherwise
touches real hardware must be checked for what happens when the *automated test suite* exercises that
same code path -- not just what happens in production use. Mirroring an existing pattern (e.g.
"Direwolf's call site already does this, so ardopcf's should too") is not sufficient justification on
its own; the two call sites can have different test exposure even when the underlying helper function
is identical. Before wiring real hardware detection into a function, grep for what tests actually
call it, and if a test exercises it directly, either keep the test path forced to `"null"`/a fake
device (e.g. via an explicit parameter override or test-only env var) or confirm affirmatively, before
running anything, that no test will trigger real hardware I/O as a side effect. When in doubt, run
nothing and ask, rather than running `cargo test` against newly-wired real-hardware code and finding
out from the user's own ears that it made noise.
