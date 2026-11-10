# SPDX-License-Identifier: AGPL-3.0-or-later
"""The restoration system: builds a directory that is a complete, restored server from backups.

It runs on a separate Debian system (the builder), never on the machine that is being restored. It reads a set of
backups (a local directory such as /storage, or Backblaze), asks for the backup passphrases on the terminal, installs the
Debian packages from the network (unpinned), restores the database and the files from the backups into the directory, and
leaves a plain directory tree that can be copied onto a freshly installed Debian with rsync or tar. `verify` boots that
tree in an isolated virtual machine with no network at all and tests that it works.

The tool holds nothing that is in the backups. Documentation: docs/RESTORATION_SYSTEM.md in hams_shared and
hams_com docs/runbooks/RESTORATION_SYSTEM.md.
"""
