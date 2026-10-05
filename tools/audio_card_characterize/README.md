# audio_card_characterize

Measure a USB (or any ALSA) sound card whose output is cabled to its own input, the same way for every
candidate card, so cards can be compared. Python 3 and numpy only (matplotlib for the plots); no compiled
code. The card is driven through `libasound` with `ctypes`, so there is nothing to build.

## What it measures (and what it cannot)

Everything is relative to digital full scale (dBFS) at the card's own converters. No voltmeter is involved:
absolute volts are not measured, and a quoted spec such as "2 Vrms" stays a spec. A loopback also cannot tell
the DAC's distortion from the ADC's, so the procedures move one stage's operating point at a time (playback
volume, capture gain) and the report reasons from how the result moves.

| command | what it does |
|---|---|
| `inspect` | every mixer control with range and dB scale, plus `/proc/asound/cardN/stream0` |
| `channels` | left-only / right-only / both / inverted: which playback channels reach the input, is the input mono |
| `levels` | ascending 1 dB staircase (default -90 to -6 dBFS) per frequency: capture level, gain, harmonic THD, THD+N, clipping; summary of 0.1/1/3 % THD, 1 and 3 dB compression and hard-clip points |
| `dynamics` | tone stepped up and back down: attack, release and undershoot of any limiter or gain control |
| `noise` | idle noise (unweighted and A-weighted), DC, spurs, 1/3-octave levels, with playback stopped / playing digital silence / output muted |
| `capsweep`, `playsweep` | tone level (and noise) at every raw value of the capture or playback control: dB per step, linearity, analog versus digital gain |
| `thdfreq`, `imd`, `response` | THD+N versus frequency at chosen capture levels; SMPTE and CCIF IMD; frequency response, -3 dB points, voice-passband ripple, group delay |
| `longrun` | a long steady tone: level, frequency (clock ratio), THD+N drift; capture and playback sample rate against `CLOCK_MONOTONIC` and `CLOCK_MONOTONIC_RAW` |
| `xruns` | underruns/overruns and read-interval jitter at small periods |
| `latency` | burst round trip: stream offset, application write-to-read, and the ALSA-delay-corrected DAC-to-ADC estimate, per period/buffer |
| `clicks` | peak deviation when playback opens/closes (after idle and quickly) and when mixer controls move |

`characterize.py --help` lists the options. Every run snapshots the mixer first and restores it when it ends,
including on SIGTERM. Output goes to `--out` as JSON (and CSV for noise spectra).

```
python3 characterize.py --card S3 --card-index 1 --out out inspect
python3 characterize.py --card S3 --out out channels --play-db -24 --cap-db 0.5
python3 characterize.py --card S3 --out out levels --play-db -24 --cap-db 0.5 --freq 1000
python3 make_plots.py out plots
./run_full_characterization.sh S3 1 out        # the complete session, about two hours
```

## Safety

* Start quiet. `levels` ramps up from -90 dBFS in blocks and stops three steps after hard clipping begins;
  its default ceiling is -6 dBFS. Raise `--hi` only after the low-level numbers are in.
* Never enable a card's mic monitor / sidetone path while its output is cabled to its input: it closes a loop.
* A desktop sound server may hold the card. Reserve it without stopping the server:
  `python3 reserve_device.py --card 1 &` (freedesktop ReserveDevice1; needs `python3-gi`), measure, then
  `kill` it. Do not touch other cards.
* Do not run this against a host that is busy with real-time audio; xrun counts will be the host's.

## Definitions used by the summaries

* Tone level: sine peak re full scale (0 dBFS = full-scale sine). Noise: RMS re the RMS of a full-scale sine.
* THD: harmonics 2 to 10 inside 20 Hz to 20 kHz, each counted only when 6 dB above the noise in its own bins
  (so noise cannot masquerade as distortion). THD+N: everything but the fundamental in the band.
* Headroom from `headroom_summary`: the playback-level distance between a capture operating point (-12 or -20
  dBFS) and the first of 1 % THD, 1 dB compression, or hard clipping.

## Tests

`python3 -m unittest tools/audio_card_characterize/test_audio_card_characterize.py` (from the repository
root). They use synthetic signals and a simulated card (`sim.py`) only; no sound hardware is opened.
