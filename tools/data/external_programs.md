# External programs the relay uses: the inventory

Machine-read data of `tools/check_external_programs_documented.py` (hams.com ADR 0114 decision 5 is the policy; this file is the
inventory it enforces). It lives beside the checker so that the check works in hams_open and hams_shared without hams.com. A pull request
that adds, removes or changes the way the relay uses an external program, or bumps a pin, updates the tables below in the same pull request.

## Inventory

### Programs the relay discovers or downloads and runs as separate processes

| Program | Role | Platforms | Obtained | Version or pin | Licence | Watched |
|---|---|---|---|---|---|---|
| Pat (`la5nta/pat`) | Winlink client | Linux, Windows, macOS (Intel .pkg, also run on Apple silicon under Rosetta 2) | pinned upstream download, SHA-256 checked (the hams.com mirror carries the same three pinned assets via `tools/relay_tool_release_pins.json`) | v1.0.1 | MIT | yes (release) |
| Mercury (`Rhizomatica/mercury`, branch `mercuryv2`) | HF modem | Windows, macOS, Linux | loopback-patched build `tnc-loopback-v1`, mirrored | commit `9bb58799ef5ea5a8afaadb2441ea4806f14583b9` plus `mercury-loopback-bind.patch` | see source tree | yes (branch) |
| ardopcf (`pflarue/ardop`, branch `develop`) | ARDOP modem | Windows, macOS, Linux | loopback-patched build `tnc-loopback-v1`, mirrored | commit `c1e11ef514ff0776fea74fb7bf7283909910af38` plus `ardopcf-loopback-bind.patch` | see source tree | yes (branch) |
| Direwolf (`wb2osz/direwolf`) | packet TNC | Windows, macOS, Linux | loopback-patched build `tnc-loopback-v1`, mirrored | commit `eda1383f5fa9d8ba3cb27f99db1d2c79494404c9` plus `direwolf-loopback-bind.patch` | GPL-2.0+ | yes (branch `master`) |
| ka9q-radio `radiod` (`ka9q/ka9q-radio`) | SDR (software-defined radio) front end of the shared receiver | Linux (Pi, servers) | installed by the operator at `/usr/local/sbin/radiod`; the relay ships `hams-radiod@.service` and an install script only | reviewed and fuzzed at commit `8c50dbbe9958c59a24e392fa1785501350e833f9` | GPL-3.0 | yes (branch `main`; held, see `dependency_watch.json`) |
| `rtl_tcp` (osmocom rtl-sdr) | RTL-SDR (a low-cost USB software-defined radio receiver) IQ source for the shared receiver | Linux | installed by the operator from the distribution `rtl-sdr` package; the relay only starts it | distribution package | GPL-2.0+ | no |
| `rigctl` / `rigctld` (Hamlib) | radio capability dump; network rig control in tests | all | with Hamlib | Debian 13 package 4.6.2-1; Windows static 4.7.2 | LGPL-2.1+ | yes (4.7.2) |

### Libraries outside the package managers that the shipped programs link

| Library | Used by | Version or pin | Licence | Watched |
|---|---|---|---|---|
| Hamlib | the relay (`libhamlib`, a package dependency on Linux; static in Windows builds) and the TNC builds | Linux packages `libhamlib4t64`/`libhamlib4`; static 4.7.2, source SHA-256 `ae1fcf2dbc80ea0786ea8f047b09399c3f7737d1930442f61a031708ed33e88f` | LGPL-2.1+ | yes (4.7.2) |
| PortAudio | macOS Direwolf, static | v19.7.0 (`pa_stable_v190700_20210406.tgz`, SHA-256 `47efbf42c77c19a05d22e627d42873e991ec0c1357219c0d74ce6a2948cb2def`) | MIT | yes (PortAudio/portaudio, v19.7.0) |
| hidapi | macOS Mercury and Direwolf, vendored in the Mercury source tree | as in the Mercury commit | BSD/GPL | no |
| libopus | Opus codec through the `opus` crate | crate 0.4 (vendored Opus build, see `Cargo.toml`) | BSD | via cargo |
| `hams-modem-packet` | native packet modem (replaces Direwolf when `HAMS_RELAY_PACKET_MODEM=native`); built from this repository and installed beside the relay executable, started by `native_packet_tnc.rs` with the path found next to the relay | ships with the relay | proprietary | no |
| `hams-modem-ardop` | native ARDOP modem (replaces ardopcf); built from this repository and installed beside the relay executable, started by `native_ardop.rs` with the path its caller passes in (the file is in `DYNAMIC_LAUNCH_FILES`) | ships with the relay | proprietary | no |
| `hams-modem-mercury` | native Mercury modem (replaces the external Mercury when `HAMS_RELAY_VARAHF_MODEM=native`); built from this repository and installed beside the relay executable, started by `native_mercury.rs` with the path its caller passes in (the file is in `DYNAMIC_LAUNCH_FILES`) | ships with the relay | proprietary | no |
| RADE / FreeDV (`rade_c`) | FreeDV RADE codec | vendored in `daemons/hams_local_relay/rade_c` | see its LICENSE | no |
| Transformers.js | the console's voice and translation, served by hams.com | `@xenova/transformers` 2.16.0 (ONNX runtime wasm files) | Apache-2.0 | yes (`huggingface/transformers.js`, release 2.16.0; held, see `dependency_watch.json`) |
| Whisper tiny, NLLB | console speech-to-text and translation models, served by hams.com | `Xenova/whisper-tiny`, NLLB (see `ml_assets.rs`) | MIT / CC-BY-NC (check before any change) | no |

### System tools and services the relay calls or depends on (not pinned)

| Platform | Programs | Used for |
|---|---|---|
| Linux and Raspberry Pi | (no `tpm2_*` tools: the relay seals secrets by sending TPM 2.0 commands to `/dev/tpmrm0` itself, `tpm2_wire.rs`; the tools are used only as the test oracle); `pkexec`; `iptables` or `nft` (the emergency hub's port 80 redirect, `hub_port_redirect.sh`); `id`; `lsof`, `netstat`; `which`, `sh`; `ping` (the hub probe loop); `traceroute` (the check-in network report; native, see below); `systemctl` (root packaging scripts only: `deb/postrm`, `rpm/post_uninstall.sh` and the `linux/install_*.sh` helpers start and stop system units as root, and `postrm` runs after the relay binary is gone; the relay's own user-service calls, `daemon-reload`, `enable --now`, `try-restart` and lingering, are made over D-Bus by `hams-local-relay --service-ctl` since 2026-11-19, and `loginctl` is no longer run), `journalctl`; `udevadm`; `dmidecode`; `gpsd` (the check-in survey and the GPS source read it over its socket; no `gpspipe` is run); `chrony` with PPS (`chronyc`); DKMS (the stock `ch341` driver); ModemManager (a udev rule keeps it off our radios); NetworkManager (the check-in Wi-Fi survey reads its access points over D-Bus, `wifi_scan.rs`; no `nmcli` is run since 2026-11-19) | sealing secrets in the TPM, privileged actions, serial permissions, device discovery, loopback-bind verification, services and autostart, GPS and time |
| Linux and Raspberry Pi, stand-alone emergency hub (broker `hub_start`, hub = true only) | `hostapd` and `dnsmasq` (distribution packages, only suggested by the relay package; the broker starts them from its own generated files with fixed arguments; licences BSD-3-Clause and GPL-2.0/3.0, run as separate programs); `ip` (above) | the access point, DHCP, DNS and DHCP option 42 of the hub (RELAY_STANDALONE_EMERGENCY_HUB.md); not pinned, the distribution's version is used |
| Windows | PowerShell; `schtasks` (the installer only: the relay's post-update relaunch runs its task through the Task Scheduler COM API, `windows_task.rs`, since 2026-11-22); `sc.exe`; `certutil`; (no `reg` and no `netstat`: the machine description and machine identity are read with the registry API, `windows_registry.rs`, and the TNC port check reads the IP Helper socket tables, `windows_sockets.rs`, since 2026-11-22); com0com (detected, not installed); virtual audio cables (detected) | installer, autostart, certificates, machine identity, virtual serial ports and audio |
| macOS | `launchctl`; `hdiutil`; `ioreg`; `system_profiler`; `sysctl`; `pkgutil`; BlackHole (detected) | installer, autostart, unpacking downloaded tools, hardware identity |

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

`bounded_command.rs` is the second: it runs whatever helper its caller hands it (`sh`, a TNC's `-h` or `--version`, and the other programs the first table already names) under a time limit and a size cap. It chooses no program itself, so the file is in `DYNAMIC_LAUNCH_FILES`; every caller that names a literal program is still checked against the tables.

`privileged_action.rs` is the third: `run_pkexec` calls `run_launcher("pkexec", ...)`, and the launcher is a parameter only so a test can stand in a fake for `pkexec`. The one program the relay starts there is `pkexec` (already in the first table), so the file is in `DYNAMIC_LAUNCH_FILES`.
