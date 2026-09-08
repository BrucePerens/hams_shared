---
name: hams-devbox-var-partition-small
description: "The hams.com dev box's /var partition is small (23G) and shared with active system services -- check disk space before Docker/apt-heavy CI verification work there."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 44fa3b1d-b189-4e0a-bf9c-d06127ace94d
  modified: 2026-09-08T04:33:53.372Z
---

The hams.com/hams_open dev box's `/var` filesystem (`/dev/nvme0n1p3`) is a small, dedicated 23G
partition, distinct from the ~2.7G `/tmp` partition already covered by the
`hams-tmp-disk-space-test-logs` memory. Docker's own storage (`/var/lib/docker`, overlay2-on-btrfs)
lives on this same partition, so pulling images and running containers for CI-verification work
(e.g. testing a GitHub Actions fix like a Wine/apt install in a clean Ubuntu container) can fill it
quickly -- a single `apt-get install wine64` plus i386 multiarch packages inside one container used
over 1.4GB, and two back-to-back attempts drove the partition from 92% to 100% full during a single
session (2026-09-07/08), with the user directly flagging both a `/tmp` cleanup need and "The /var
filesystem is full" as it happened.

This partition also hosts real, currently-active system state that isn't mine to clean up or
disrupt: `/var/lib/odoo` (~11G, the real Odoo filestore), and `/var/lib/waydroid` (~3.2G) backing a
live, enabled `waydroid-container.service` -- confirmed active via `systemctl is-active`, not dead
leftover files, so it must be left alone even though it is large and looks like an obvious space
target. When freeing space, safe, always-appropriate targets are: `docker system prune -af`
(dangling images/build cache; add `--volumes` only after confirming with `docker volume ls` that
nothing named and still in use would be swept, since an unattached-looking named volume can survive
prune anyway but this isn't guaranteed), `sudo apt-get clean` (package cache), and any of my own
prior scratch/log files under `/tmp` or `/var/tmp` I created this session.

The practical lesson: before starting Docker-based CI-fix verification (pulling a base image,
installing a heavy package set like Wine or a full toolchain) on this dev box, check `df -h /var`
first, and re-check between attempts rather than retrying blindly after a failure -- a failure whose
real cause is local disk exhaustion (`E: Write error - write (28: No space left on device)`,
`dpkg: error: ... No space left on device`) is not evidence about whether the underlying CI fix is
correct, and repeating the same heavy install without freeing space first just fails the same way
again while continuing to risk the other real services sharing this small partition. When local
verification genuinely cannot be completed within this partition's real capacity, it is more
responsible to disclose that honestly and trust a well-reasoned fix's actual verification to run on
GitHub's own runner (which has ample disk) than to keep consuming shared local disk on repeated
failed attempts.
