# ADR 0114: The External Programs the Relay Uses Are Inventoried, Pinned and Watched

## Status
Accepted (Bruce, 2026-11-03). The inventory below was compiled on 2026-11-03 from the relay source
(`daemons/hams_local_relay/src`, programs launched outside tests), its packaging and installers, the release
scripts, `dependency_watch.json`, `binary_manifest.json` and `packaging/tnc_loopback_release/SOURCE.txt`.

## Scope
Programs and tools that are not our own code and are not Cargo or pip packages: programs the relay starts, the
libraries and toolchains outside the package managers that its builds and installers depend on, and the
assets it downloads. Cargo crates are governed by `Cargo.lock`, `cargo-deny` and `cargo-audit`; Python
packages by pip-audit. The server-side and test-infrastructure programs are listed briefly at the end because
the same weekly watch covers some of them.

## Context
The relay launches modems, a radio library, audio and security tools; it is installed on Windows, macOS,
Linux and the Raspberry Pi; and its builds use several toolchains. Until now the list lived in people's
heads and in scattered comments, so a new program could be added without anyone noticing, an upstream
release could pass unseen (the weekly watch found four stale pins on 2026-11-03), and Direwolf and ka9q-radio
had no pin or watch at all. Two rules from `CLAUDE.md` also depend on knowing what runs: the radio modem
programs must bind loopback in the builds we ship (the patched build is the fix, never a firewall rule), and
a download is trusted only through hams.com's reviewed mirror with a checked hash.

## Decision
1. **This ADR is the inventory.** Every external program the relay launches or depends on appears in the tables
   below with its role, the platforms it is used on, how it is obtained, its version or pin, its licence, and
   whether the weekly watch covers it. A pull request that adds, removes or changes the way the relay uses an
   external program, or bumps a pin, updates the table in the same pull request.
2. **Pin what we ship or build.** A program we build, bundle or download is pinned to an exact version or
   commit, listed in `hams_shared/tools/dependency_watch.json` (or `binary_manifest.json` for hashed
   binaries), and checked against upstream by the weekly Dependency Release Watch. A stale pin is fixed or
   recorded as a deliberate hold with a reason; the watch is not silenced.
3. **System tools are named, not pinned.** Programs the operating system or the operator provides (shells,
   `pactl`, `nmcli`, `gpsd`, `chrony`, TPM tools, `launchctl`, PowerShell) are listed with the minimum
   behaviour the relay relies on. The relay detects them, tolerates their absence and reports what is missing.
4. **Downloaded tools come from the hams.com mirror** (`ham.relay.tool.release`), reviewed by a maintainer,
   with the SHA-256 the server reports checked on install. Stock builds of the radio modem programs are never
   installed; only our loopback-patched builds (release `tnc-loopback-v1` and its successors) with the
   corresponding source published beside them.
5. **Enforcement.** `hams_shared/tools/check_external_programs_documented.py`, a step of `run_linters.py`, scans the relay
   source (test code left out) and the installers for programs launched or detected by a literal name and fails when a
   name is missing from the tables here; it fails when a table row that names an `owner/repo` is absent from
   `dependency_watch.json` or not marked watched, and when a watch entry is absent here. It has no baseline. Programs
   that an installer or script starts without detecting them first (`cp`, `tar`, `curl`, and so on) are not scanned;
   review covers them.

## Inventory

### Programs the relay discovers or downloads and runs as separate processes

| Program | Role | Platforms | Obtained | Version or pin | Licence | Watched |
|---|---|---|---|---|---|---|
| Pat (`la5nta/pat`) | Winlink client | Linux, Windows (macOS: not installable yet) | managed download from the hams.com mirror, SHA-256 checked | v1.0.1 | MIT | yes (release) |
| Mercury (`Rhizomatica/mercury`, branch `mercuryv2`) | HF modem | Windows, macOS, Linux | loopback-patched build `tnc-loopback-v1`, mirrored | commit `9bb58799ef5ea5a8afaadb2441ea4806f14583b9` plus `mercury-loopback-bind.patch` | see source tree | yes (branch) |
| ardopcf (`pflarue/ardop`, branch `develop`) | ARDOP modem | Windows, macOS, Linux | loopback-patched build `tnc-loopback-v1`, mirrored | commit `c1e11ef514ff0776fea74fb7bf7283909910af38` plus `ardopcf-loopback-bind.patch` | see source tree | yes (branch) |
| Direwolf (`wb2osz/direwolf`) | packet TNC | Windows, macOS, Linux | loopback-patched build `tnc-loopback-v1`, mirrored | commit `eda1383f5fa9d8ba3cb27f99db1d2c79494404c9` plus `direwolf-loopback-bind.patch` | GPL-2.0+ | yes (branch `master`) |
| ka9q-radio `radiod` (`ka9q/ka9q-radio`) | SDR front end of the shared receiver | Linux (Pi, servers) | installed by the operator at `/usr/local/sbin/radiod`; the relay ships `hams-radiod@.service` and an install script only | reviewed and fuzzed at commit `8c50dbbe9958c59a24e392fa1785501350e833f9` | GPL-3.0 | yes (branch `main`; held, see `dependency_watch.json`) |
| `rigctl` / `rigctld` (Hamlib) | radio capability dump; network rig control in tests | all | with Hamlib | Debian 13 package 4.6.2-1; Windows static 4.7.2 | LGPL-2.1+ | yes (4.7.2) |

### Libraries outside the package managers that the shipped programs link

| Library | Used by | Version or pin | Licence | Watched |
|---|---|---|---|---|
| Hamlib | the relay (`libhamlib`, a package dependency on Linux; static in Windows builds) and the TNC builds | Linux packages `libhamlib4t64`/`libhamlib4`; static 4.7.2, source SHA-256 `ae1fcf2dbc80ea0786ea8f047b09399c3f7737d1930442f61a031708ed33e88f` | LGPL-2.1+ | yes (4.7.2) |
| PortAudio | macOS Direwolf, static | v19.7.0 (`pa_stable_v190700_20210406.tgz`, SHA-256 `47efbf42c77c19a05d22e627d42873e991ec0c1357219c0d74ce6a2948cb2def`) | MIT | yes (PortAudio/portaudio, v19.7.0) |
| hidapi | macOS Mercury and Direwolf, vendored in the Mercury source tree | as in the Mercury commit | BSD/GPL | no |
| libopus | Opus codec through the `opus` crate | crate 0.4 (vendored Opus build, see `Cargo.toml`) | BSD | via cargo |
| RADE / FreeDV (`rade_c`) | FreeDV RADE codec | vendored in `daemons/hams_local_relay/rade_c` | see its LICENSE | no |
| Transformers.js | the console's voice and translation, served by hams.com | `@xenova/transformers` 2.16.0 (ONNX runtime wasm files) | Apache-2.0 | no |
| Whisper tiny, NLLB | console speech-to-text and translation models, served by hams.com | `Xenova/whisper-tiny`, NLLB (see `ml_assets.rs`) | MIT / CC-BY-NC (check before any change) | no |

### System tools and services the relay calls or depends on (not pinned)

| Platform | Programs | Used for |
|---|---|---|
| Linux and Raspberry Pi | `amixer`, `arecord`, `pactl` (alsa-utils, PulseAudio or PipeWire); `tpm2_create`, `tpm2_createprimary`, `tpm2_load`, `tpm2_unseal`, `tpm2_hmac`, `tpm2_readpublic`; `pkexec`; `id`; `modprobe`; `lsof`, `netstat`; `which`, `sh`; `nmcli`; `ip`, `traceroute` (the check-in network report); `systemctl`, `loginctl`, `journalctl`; `udevadm`; `dmidecode`; `gpsd`/`gpspipe`; `chrony` with PPS; DKMS (the stock `ch341` driver); ModemManager (a udev rule keeps it off our radios); NetworkManager | audio devices and mixers, sealing secrets in the TPM, privileged actions, serial permissions, device discovery, loopback-bind verification, services and autostart, GPS and time |
| Linux and Raspberry Pi, stand-alone emergency hub (broker `hub_start`, hub = true only) | `hostapd` and `dnsmasq` (distribution packages, only suggested by the relay package; the broker starts them from its own generated files with fixed arguments; licences BSD-3-Clause and GPL-2.0/3.0, run as separate programs); `ip` (above) | the access point, DHCP, DNS and DHCP option 42 of the hub (RELAY_STANDALONE_EMERGENCY_HUB.md); not pinned, the distribution's version is used |
| Windows | PowerShell; `schtasks`; `sc.exe`; `certutil`; `reg`; com0com (detected, not installed); virtual audio cables (detected) | installer, autostart, certificates, machine identity, virtual serial ports and audio |
| macOS | `launchctl`; `hdiutil`, `xar`, `cpio`, `gunzip`; `ioreg`; `system_profiler`; `pkgutil`; BlackHole (detected) | installer, autostart, unpacking downloaded tools, hardware identity |

### Build and release toolchain

| Tool | Used for | Version or pin | Watched |
|---|---|---|---|
| Rust toolchain | all builds | 1.98.1 (`rust-toolchain.toml`; targets `armv7-unknown-linux-gnueabihf`, `x86_64-pc-windows-gnu`) | no (pinned by hand) |
| MinGW-w64 | Windows cross-build | gcc 14, posix threads | no |
| Inno Setup (under wine on Linux) | Windows installer | 6.7 | no |
| Ubuntu 22.04 container | Linux x86_64 and aarch64 release builds | glibc 2.35, gcc 11 | no |
| Debian 12 (bookworm) root file system | Raspberry Pi builds on `mac1` | glibc 2.36 | no |
| Apple clang | macOS builds | 21 (Command Line Tools for Xcode 27.0), on `mac1` | no |
| python3 | signing the self-extracting installer payload (`release/release_sig.py`), and verifying it on the user machine (OpenSSL 3 is the fallback) | system version | no |
| zipsign, dpkg/rpm tooling, `apt-ftparchive` | signing the updater bundle, repositories | system versions (`build_release.sh` preflight) | no |

### Server and test infrastructure that the same watch covers

| Program | Role | Version or pin | Watched |
|---|---|---|---|
| kopia | backups | v0.23.1 (`binary_manifest.json`) | yes |
| etcd | coordination | v3.7.3 (`binary_manifest.json`) | yes |
| cloudflared | tunnel | 2026.10.7 (`binary_manifest.json`) | yes |
| Odoo, PostgreSQL, Redis, RabbitMQ, Node, Go, Chrome or Chromium with Playwright | production and the test runners | see `hams_shared/tools/provision_build_host.sh`, `requirements-test.txt` and `docs/runbooks/TEST_RUNNERS.md` | pip-audit and package updates |

### Tools the installers and release scripts detect (`command -v`, `Get-Command`)

| Platform | Programs | Used for |
|---|---|---|
| Linux installers | `sudo`; `apt-get`, `dnf`, `pacman`; `ldd`, `ldconfig`; `gpg`; `chronyd`; `dkms`, `mokutil` | privilege, package managers of the three distribution families, finding missing shared libraries, checking the repository signing key, GPS time, building and signing the USB serial driver |
| Release builds | `cargo`, `cargo-deb`, `cargo-generate-rpm`, `innoextract` | building the packages and checking the Windows installer's contents |

## Discovery functions (launches whose program name is a variable)
`find_pat_binary` (`winlink.rs`), `find_direwolf_binary` (`direwolf.rs`), `find_mercury_binary` (`mercury.rs`) and
`find_ardopcf_binary` (`ardop.rs`) each locate one managed, pinned program from the first table and nothing else; the
relay starts that path with `Command::new(<variable>)`. The enforcement linter requires all four to exist and to be
named here, so a new discovery function (or a rename) is a change to this ADR.

The device worker (`device_worker_launch.rs`) is the one launch whose program is not ours to pin: the operator names it in
`HAMS_LOCAL_RELAY_DEVICE_WORKER_COMMAND` (unset means nothing starts), so the file is in `DYNAMIC_LAUNCH_FILES`.

## Known gaps (to-dos)
- hidapi, RADE, Transformers.js and the model files have no upstream check.
- Inno Setup, MinGW-w64, wine and the container images are not pinned beyond what the release preflight
  prints; record the exact versions the next release uses.
- Pat is not installable on macOS; the mirror has no macOS build yet.

## Consequences
One place to look when a security advisory, a licence question or a build failure names a program; each change
to the set of programs is visible in review; the weekly watch becomes the complete list of what can go stale.
The cost is keeping the table current, which the enforcement linter will check.
