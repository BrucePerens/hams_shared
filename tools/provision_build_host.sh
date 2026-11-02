#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Provision a Debian machine as a lint, build and test runner for run_linters.py --remote
# (tools/lint_remote.py, tools/lint_machines.json). Runs ON the machine, as the unprivileged
# lint user (`ai`), with `sudo -n` for apt only; nothing else is touched (no desktop, power,
# display, VM, USB or service settings).
#
#   provision_build_host.sh plan     what would be installed (default; changes nothing)
#   provision_build_host.sh apply    install what is missing; idempotent, safe to repeat
#   provision_build_host.sh verify   prove the machine can cargo test, run node tests, run
#                                    pytest -n and take a headless Chrome screenshot
#   provision_build_host.sh versions print the versions for tools/lint_machines.json
#   provision_build_host.sh undo     remove exactly what `apply` recorded as its own
#
# Used by `python3 tools/lint_remote.py provision HOST --apply`, which copies this file and
# tools/requirements-test.txt to the machine and runs it. Environment:
#   HAMS_RUST_TOOLCHAIN   toolchain to install (default 1.98.1: the `channel` of every
#                         rust-toolchain.toml in hams_open and hams_com; the caller passes the pin it read)
#   HAMS_REQUIREMENTS     path of requirements-test.txt (default: next to this script)
#
# What `apply` installs
#   apt (needs sudo -n; only packages that are not installed yet; none is ever upgraded or removed
#        except by `undo`, and only those this script installed):
#     build-essential pkg-config cmake clang autoconf automake libtool   C/C++ toolchain; the vendored opus
#     libssl-dev libusb-1.0-0-dev libasound2-dev libudev-dev libhamlib-dev   openssl, rusb, cpal/alsa, serialport, hamlib-oracle
#     git rsync curl ca-certificates tar                                 sync of the mirror, rustup
#     python3-pip python3-venv python3-httpx mypy                        pip; httpx and mypy (hams_shared tools tests use them)
#     nodejs npm                                                         console and JavaScript checks (Debian's node 20)
#     chromium fonts-liberation fonts-dejavu-core   only when neither google-chrome nor chromium exists
#   user level, under $HOME (no sudo):
#     ~/.cargo ~/.rustup   rustup (installer SHA-256 verified) with the pinned toolchain, rustfmt, clippy
#     ~/.local/lib/python3.*/site-packages   pytest, pytest-timeout, pytest-xdist, flake8 from
#                          requirements-test.txt (pure wheels pinned by SHA-256)
#   state: ~/.cache/hams-lint/provision/ (what this script installed, for undo)
set -u

TOOLCHAIN="${HAMS_RUST_TOOLCHAIN:-1.98.1}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQS="${HAMS_REQUIREMENTS:-$HERE/requirements-test.txt}"
STATE="$HOME/.cache/hams-lint/provision"
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
export DEBIAN_FRONTEND=noninteractive

APT_PACKAGES=(build-essential pkg-config cmake clang autoconf automake libtool
  libssl-dev libusb-1.0-0-dev libasound2-dev libudev-dev libhamlib-dev
  git rsync curl ca-certificates tar python3-pip python3-venv python3-httpx mypy nodejs npm)
CHROME_PACKAGES=(chromium fonts-liberation fonts-dejavu-core)

say() { printf '%s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
apt_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q '^install ok installed'; }
has_chrome() { have google-chrome || have google-chrome-stable || have chromium || have chromium-browser; }
chrome_bin() { for c in google-chrome google-chrome-stable chromium chromium-browser; do have "$c" && { command -v "$c"; return; }; done; }

missing_apt() {
  local p
  for p in "${APT_PACKAGES[@]}"; do apt_installed "$p" || printf '%s\n' "$p"; done
  if ! has_chrome; then for p in "${CHROME_PACKAGES[@]}"; do apt_installed "$p" || printf '%s\n' "$p"; done; fi
}

rustup_has_toolchain() { have rustup && rustup toolchain list 2>/dev/null | grep -q "^$TOOLCHAIN"; }

pip_missing() {
  python3 - "$REQS" <<'PY'
import re, sys
from importlib import metadata
for line in open(sys.argv[1], encoding="utf-8"):
    m = re.match(r"([A-Za-z0-9_.-]+)==([^\s\\]+)", line)
    if not m:
        continue
    try:
        if metadata.version(m.group(1)) == m.group(2):
            continue
    except metadata.PackageNotFoundError:
        pass
    print(f"{m.group(1)}=={m.group(2)}")
PY
}

do_plan() {
  say "provision_build_host.sh plan for $(hostname) ($(uname -m)), toolchain $TOOLCHAIN"
  local miss; miss="$(missing_apt | tr '\n' ' ')"
  say "apt packages to install (sudo -n): ${miss:-none}"
  if rustup_has_toolchain; then say "rust: toolchain $TOOLCHAIN already installed"; else say "rust: rustup + toolchain $TOOLCHAIN with rustfmt and clippy (user level)"; fi
  local pm; pm="$(pip_missing | tr '\n' ' ')"
  say "python packages (user level, hash-pinned): ${pm:-none}"
  has_chrome && say "browser: $(chrome_bin) already present" || say "browser: chromium (apt)"
  say "undo: provision_build_host.sh undo (removes only what apply recorded in $STATE)"
}

record() { mkdir -p "$STATE"; printf '%s\n' "$2" >>"$STATE/$1"; }

do_apt() {
  local miss; miss="$(missing_apt | tr '\n' ' ')"
  [ -z "$miss" ] && { say "apt: nothing missing"; return 0; }
  say "apt: installing $miss"
  sudo -n true 2>/dev/null || { say "apt: sudo -n is not available; install by hand: $miss"; return 1; }
  sudo -n env DEBIAN_FRONTEND=noninteractive apt-get update -qq || say "apt-get update failed (continuing with the cached lists)"
  # shellcheck disable=SC2086
  sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends $miss || return 1
  local p; for p in $miss; do record apt-installed.txt "$p"; done
}

do_rust() {
  if ! have rustup; then
    local arch triple dir
    arch="$(uname -m)"; triple="${arch}-unknown-linux-gnu"
    dir="$(mktemp -d)"
    curl --proto '=https' --tlsv1.2 -fsSL "https://static.rust-lang.org/rustup/dist/$triple/rustup-init" -o "$dir/rustup-init" || return 1
    curl --proto '=https' --tlsv1.2 -fsSL "https://static.rust-lang.org/rustup/dist/$triple/rustup-init.sha256" -o "$dir/rustup-init.sha256" || return 1
    local want got
    want="$(cut -d' ' -f1 "$dir/rustup-init.sha256")"; got="$(sha256sum "$dir/rustup-init" | cut -d' ' -f1)"
    [ "$want" = "$got" ] || { say "rustup-init checksum mismatch ($want vs $got)"; return 1; }
    chmod +x "$dir/rustup-init"
    "$dir/rustup-init" -y --no-modify-path --profile minimal --default-toolchain none || return 1
    rm -rf "$dir"
    record rustup-installed.txt "$HOME/.cargo/bin/rustup"
  fi
  if ! rustup_has_toolchain; then
    rustup toolchain install "$TOOLCHAIN" --profile minimal -c rustfmt -c clippy || return 1
  fi
  rustup default "$TOOLCHAIN" >/dev/null 2>&1 || true
}

do_pip() {
  local pm; pm="$(pip_missing)"
  [ -z "$pm" ] && { say "python: nothing missing"; return 0; }
  say "python: installing $(echo "$pm" | tr '\n' ' ')"
  python3 -m pip install --user --break-system-packages --require-hashes --only-binary :all: -r "$REQS" || return 1
  local p; for p in $pm; do record pip-installed.txt "${p%%==*}"; done
}

# The backup tests create scratch databases: on a machine that already runs PostgreSQL (the Odoo test VM) the
# lint user needs a role that may do that. Nothing is created where PostgreSQL is absent.
do_pgrole() {
  have psql && have pg_lsclusters && [ -n "$(pg_lsclusters -h 2>/dev/null)" ] || return 0
  local me; me="$(id -un)"
  if sudo -n -u postgres psql -Atc "select 1 from pg_roles where rolname='$me'" 2>/dev/null | grep -q 1; then return 0; fi
  sudo -n -u postgres createuser --createdb "$me" 2>/dev/null && { say "postgres: role $me (createdb) created"; record pgrole-created.txt "$me"; }
}

do_apply() {
  local rc=0
  do_apt || rc=1
  do_pgrole
  do_rust || rc=1
  do_pip || rc=1
  do_versions
  return $rc
}

do_versions() {
  say "versions on $(hostname):"
  say "  os: $(. /etc/os-release && echo "$PRETTY_NAME") $(uname -m), $(nproc) cores, $(awk '/MemTotal/ {printf "%.0f GiB", $2/1048576}' /proc/meminfo)"
  say "  gcc: $(gcc --version 2>/dev/null | head -1)"
  say "  rustc: $(rustc --version 2>/dev/null)  cargo: $(cargo --version 2>/dev/null)  clippy: $(cargo clippy --version 2>/dev/null)  rustfmt: $(rustfmt --version 2>/dev/null)"
  say "  node: $(node --version 2>/dev/null)  npm: $(npm --version 2>/dev/null)"
  say "  python: $(python3 --version 2>/dev/null)  pytest: $(python3 -m pytest --version 2>&1 | head -1)  xdist: $(python3 -c 'import xdist;print(xdist.__version__)' 2>/dev/null)  flake8: $(python3 -m flake8 --version 2>/dev/null | head -1)"
  say "  git: $(git --version 2>/dev/null)  rsync: $(rsync --version 2>/dev/null | head -1)"
  local c; c="$(chrome_bin)"; say "  browser: ${c:-none} $([ -n "$c" ] && "$c" --version 2>/dev/null | head -1)"
}

do_verify() {
  local rc=0 d
  d="$(mktemp -d)"
  trap 'rm -rf "$d"' RETURN
  say "== cargo test of a small crate"
  ( cd "$d" && cargo new --lib --vcs none -q smoke && cd smoke \
    && printf '#[cfg(test)]\nmod t {\n    #[test]\n    fn adds() {\n        assert_eq!(2 + 2, 4);\n    }\n}\n' >>src/lib.rs \
    && cargo test -q 2>&1 | tail -3 && cargo clippy -q -- -D warnings && cargo fmt --check ) || { say "FAIL cargo"; rc=1; }
  say "== node test"
  printf 'const t = require("node:test"); const a = require("node:assert");\nt("adds", () => a.strictEqual(2 + 2, 4));\n' >"$d/smoke.test.js"
  node --test "$d/smoke.test.js" 2>&1 | grep -E '^# (pass|fail)' || { say "FAIL node"; rc=1; }
  say "== pytest with xdist"
  printf 'def test_a():\n    assert 1 + 1 == 2\n\n\ndef test_b():\n    assert "a" in "cat"\n' >"$d/test_smoke_a.py"
  printf 'def test_c():\n    assert [1] == [1]\n' >"$d/test_smoke_b.py"
  ( cd "$d" && python3 -m pytest -q -n 2 --dist loadfile test_smoke_a.py test_smoke_b.py 2>&1 | tail -2 ) || { say "FAIL pytest"; rc=1; }
  python3 -m flake8 --version >/dev/null 2>&1 || { say "FAIL flake8"; rc=1; }
  say "== headless browser screenshot"
  local c; c="$(chrome_bin)"
  if [ -z "$c" ]; then say "FAIL no browser"; rc=1; else
    "$c" --headless --no-sandbox --disable-gpu --hide-scrollbars --window-size=400,300 --screenshot="$d/shot.png" \
      'data:text/html,<body style="background:green"><h1>hams</h1></body>' >/dev/null 2>&1
    if [ -s "$d/shot.png" ] && head -c 4 "$d/shot.png" | grep -q PNG; then say "ok: $(wc -c <"$d/shot.png") byte PNG from $c"; else say "FAIL screenshot"; rc=1; fi
  fi
  [ $rc -eq 0 ] && say "VERIFY OK" || say "VERIFY FAILED"
  return $rc
}

do_undo() {
  local p
  if [ -s "$STATE/pip-installed.txt" ]; then
    # shellcheck disable=SC2046
    python3 -m pip uninstall -y --break-system-packages $(sort -u "$STATE/pip-installed.txt") || true
    rm -f "$STATE/pip-installed.txt"
  fi
  if [ -s "$STATE/rustup-installed.txt" ]; then rustup self uninstall -y || true; rm -f "$STATE/rustup-installed.txt"; fi
  if [ -s "$STATE/pgrole-created.txt" ]; then
    while read -r p; do sudo -n -u postgres dropuser "$p" || true; done <"$STATE/pgrole-created.txt"
    rm -f "$STATE/pgrole-created.txt"
  fi
  if [ -s "$STATE/apt-installed.txt" ]; then
    # shellcheck disable=SC2046
    sudo -n apt-get remove -y -qq $(sort -u "$STATE/apt-installed.txt") || true
    rm -f "$STATE/apt-installed.txt"
  fi
  say "undone: only what apply recorded in $STATE"
}

case "${1:-plan}" in
  plan) do_plan ;;
  apply) do_apply ;;
  verify) do_verify ;;
  versions) do_versions ;;
  undo) do_undo ;;
  *) say "usage: $0 plan|apply|verify|versions|undo"; exit 2 ;;
esac
