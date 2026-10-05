#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# One complete characterization session of a looped-back card (the run used for the Sound Blaster Play! 3 report).
# usage: run_full_characterization.sh <ALSA card id> <card index> <out dir>
# Every step restores the mixer when it ends.  Expect about two hours; use `nice` and a quiet machine.
# Settings below are for a card whose controls are named like the Play! 3's; adjust --play-ctl/--cap-ctl otherwise.
CARD=${1:?card id}; IDX=${2:?card index}; OUT=${3:?out dir}
HERE=$(cd "$(dirname "$0")" && pwd)
run() { echo "=== $(date +%T) $*"; nice timeout 3600 python3 "$HERE/characterize.py" --card "$CARD" --card-index "$IDX" --out "$OUT" "$@" || echo "FAILED: $*"; }

run inspect
run channels --play-db -23.5 --cap-db 0.5
# the limiter / gain-control behaviour: attack and release, at several capture gains
for CAP in 0.5 10 20; do run dynamics --play-db -12 --cap-db $CAP --low -40 --high -10; done
# the level staircase grid: playback volume x capture gain (1 kHz)
for SPK in -44 -30 -23.5 -12 -6 0; do
  HI=0; [ "$SPK" = "-6" ] && HI=-6; [ "$SPK" = "0" ] && HI=-12      # ramp rule: never above these with a high output volume
  for CAP in 0.5 10 20 30; do run levels --play-db $SPK --cap-db $CAP --freq 1000 --hi $HI; done
done
# the other frequencies at two operating points
run levels --play-db -30 --cap-db 20 --freq 300,1500,3000,6000 --hi 0
run levels --play-db -23.5 --cap-db 0.5 --freq 300,1500,3000,6000 --hi 0
# noise: capture gain 0.5 dB and the as-found 30 dB, three playback states each
run noise --play-db -23.5 --cap-db 0.5 --seconds 30
run noise --play-db -23.5 --cap-db 30 --seconds 30
run capsweep --play-db -23.5 --level -50
run playsweep --cap-db 0.5 --level -40
# distortion and response at the as-found capture gain
run thdfreq --play-db -23.5 --cap-db 30
run imd --play-db -23.5 --cap-db 30
run response --play-db -23.5 --cap-db 30
run latency --play-db -23.5 --cap-db 30
run clicks --play-db -23.5 --cap-db 0.5 --level -12
mv "$OUT/clicks.json" "$OUT/clicks_cap0p5.json"
run clicks --play-db -23.5 --cap-db 30 --level -32.5
run longrun --play-db -23.5 --cap-db 30 --level -32.5 --seconds 900
run xruns --play-db -23.5 --cap-db 30 --seconds 300 --periods 64,128,256
echo "=== done $(date +%T)"
