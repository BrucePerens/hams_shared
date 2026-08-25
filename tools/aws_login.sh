#!/usr/bin/env bash
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# A helper for driving `aws login --remote` (the AWS CLI v2 browser-SSO
# login added for the AWS Agent Toolkit / AI-coding-agent setup flow)
# from an AI agent or any other caller that can't sit in front of a
# single interactive terminal for the whole login -- e.g. an agent
# running on a remote/headless box via SSH, where step 1 (start the
# login, get a URL) and step 2 (a human completes the browser flow and
# hands back a code) happen in separate calls, possibly minutes apart.
#
# Two problems this solves:
#
# 1. Timing. `aws login --remote` prints an authorize URL and then
#    blocks on stdin waiting for the code. If you don't already have the
#    process's stdin held open by something that won't close early, any
#    naive "start it, then pipe the code in later" approach races: the
#    reader either isn't ready yet, or whatever held its stdin open
#    exits and delivers EOF before the human finishes the browser flow.
#    This script sidesteps that entirely with a self-referencing FIFO
#    open (`exec 3<>fifo`): opening a FIFO for read-write, by itself,
#    keeps it open indefinitely without blocking and without ever
#    seeing EOF, no matter how long the human takes.
#
# 2. The actual code format. The CLI's prompt says "Enter the
#    authorization code displayed in your browser", and the browser's
#    confirmation page displays "code=<value>&state=<uuid>". It is
#    tempting -- and wrong -- to paste that whole string in. The CLI
#    wants only <value> (a compact JWE: header.encrypted_key.iv.
#    ciphertext.tag, 5 dot-separated segments with an empty
#    encrypted_key segment for alg:dir). Feed it the wrapped
#    "code=...&state=..." string instead and it fails every time with
#    "Failed to decode the verification code" -- a real, reproducible,
#    completely misleading error that gives no hint that the code was
#    otherwise perfectly valid and the only problem was the wrapper
#    around it. This cost real time to track down the first time
#    (multiple full browser-login round trips, several different code-
#    delivery mechanisms all ruled out as red herrings, before the
#    actual cause -- the wrapper, not corruption -- was confirmed by
#    checking the segment count directly). `feed` strips the wrapper
#    automatically and warns if what's left still isn't 5 segments, so
#    that diagnosis is never needed again.
#
# Usage:
#   aws_login.sh start <region> <profile>
#   aws_login.sh feed <<< 'code=...&state=...'   # or a bare code
#
# Holds no secrets itself -- region/profile are parameters, and it only
# ever handles a single-use, short-lived OAuth authorization code, never
# a long-lived credential. The credentials `aws login` itself produces
# are cached by the AWS CLI in its own standard location, not by this
# script.
set -euo pipefail

SCRATCH="${AWS_LOGIN_SCRATCH:-$HOME/.local/state/aws-login}"
FIFO="$SCRATCH/fifo"
OUT="$SCRATCH/out.log"

cmd_start() {
    local region="${1:?Usage: aws_login.sh start <region> <profile>}"
    local profile="${2:?Usage: aws_login.sh start <region> <profile>}"

    mkdir -p "$SCRATCH"
    rm -f "$FIFO"
    mkfifo "$FIFO"
    rm -f "$OUT"

    # A self-referencing read-write open (exec 3<>FIFO) never sees EOF,
    # so the login process waits indefinitely for a code -- no race
    # against a separate writer process closing too early.
    nohup bash -c "exec 3<>'$FIFO'; aws login --region '$region' --profile '$profile' --remote <&3" \
        > "$OUT" 2>&1 &
    disown

    for _ in $(seq 1 20); do
        sleep 0.5
        grep -q "https://" "$OUT" 2>/dev/null && break
    done
    cat "$OUT"
}

cmd_feed() {
    if [ ! -p "$FIFO" ]; then
        echo "No waiting login session found at $FIFO. Run '$0 start <region> <profile>' first." >&2
        exit 1
    fi

    local raw code segments
    raw="$(cat)"
    code="$(printf '%s' "$raw" | sed -E 's/^code=//; s/&state=.*$//')"

    if [ -z "$code" ]; then
        echo "Extracted an empty code from the input -- refusing to feed it." >&2
        exit 1
    fi

    # JWE compact serialization: header.encrypted_key.iv.ciphertext.tag
    # (5 dot-separated segments; encrypted_key is empty for alg:dir).
    # This catches a corrupted/truncated code before it burns another
    # round trip through the browser.
    segments="$(printf '%s' "$code" | awk -F. '{print NF}')"
    if [ "$segments" != "5" ]; then
        echo "Warning: extracted code has $segments dot-separated segments, expected 5 (JWE compact form). Feeding it anyway, but this will likely fail." >&2
    fi

    printf '%s\n' "$code" > "$FIFO"
    echo "Code fed. Check $OUT for the result." >&2
}

case "${1:-}" in
    start) shift; cmd_start "$@" ;;
    feed) cmd_feed ;;
    *)
        echo "Usage: $0 start <region> <profile>  |  $0 feed <<< 'code=...&state=...'" >&2
        exit 1
        ;;
esac
