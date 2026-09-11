#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Hashes a password using the same PBKDF2-SHA512 scheme Odoo's own
res.users._crypt_context() uses, so the result can be injected directly
into res_users.password via raw SQL at init time (see
ham_init/hooks.py:post_init_hook, ODOO_ADMIN_PASSWORD) without Odoo's ORM
ever seeing -- and therefore never needing -- the plaintext.

Per MASTER_10_IDENTITY_ACCESS_CONTROL.md, the master database password
must never be stored in plaintext; this is the utility that ADR
references for producing the pre-hashed value.
"""

import argparse
import getpass
import sys

from passlib.hash import pbkdf2_sha512

# Odoo's own MIN_ROUNDS floor (odoo/addons/base/models/res_users.py), used
# whenever the password.hashing.rounds config parameter isn't set higher.
DEFAULT_ROUNDS = 600_000


def hash_password(password, rounds=DEFAULT_ROUNDS):
    return pbkdf2_sha512.using(rounds=rounds).hash(password)


def _strip_line_ending(line):
    """Strips exactly one trailing line-ending sequence ("\\r\\n" or a bare "\\n"), never a bare
    trailing "\\r" alone and never more than one line ending.

    Real bug found 2026-09-10: piped-input password reading used to do `.rstrip("\\n")`, which
    strips every trailing "\\n" character but leaves a trailing "\\r" completely untouched. Piped
    input with CRLF line endings (a real, plausible source: a Windows-authored secrets file, a
    `.env`-style file saved with CRLF, a value copy-pasted through a CRLF-preserving clipboard) --
    e.g. `printf "pass\\r\\n" | hash_admin_password.py` -- would silently hash "pass\\r" instead of
    "pass". This tool exists specifically so the master admin password is never handled in
    plaintext by Odoo's own ORM (see this file's own module docstring); a hash silently computed
    over "pass\\r" instead of the real, intended "pass" would make the eventual real login attempt
    (typed without the invisible trailing \\r) fail against the stored hash, with no visible clue
    why -- the exact kind of silent, hard-to-diagnose corruption this tool's whole purpose is to
    avoid introducing. `.rstrip("\\r\\n")` was considered and rejected: it would strip an
    arbitrary NUMBER of trailing \\r/\\n characters, which could still eat a legitimate trailing
    character if a password ever genuinely ended in one (vanishingly unlikely, but this function
    strips exactly one real line-ending sequence and nothing more, matching how a text-mode reader
    presents "one line" without its terminator)."""
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith("\n"):
        return line[:-1]
    return line


def main():
    parser = argparse.ArgumentParser(
        description="Hash a password for ODOO_ADMIN_PASSWORD / ODOO_SERVICE_PASSWORD "
        "using Odoo's own PBKDF2-SHA512 scheme."
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=DEFAULT_ROUNDS,
        help=f"PBKDF2 round count (default: {DEFAULT_ROUNDS}, Odoo's own MIN_ROUNDS floor).",
    )
    args = parser.parse_args()

    if sys.stdin.isatty():
        password = getpass.getpass("Password to hash: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords did not match.", file=sys.stderr)
            sys.exit(1)
    else:
        # Piped input, e.g. `echo -n "$PASS" | hash_admin_password.py`.
        password = _strip_line_ending(sys.stdin.readline())

    if not password:
        print("Refusing to hash an empty password.", file=sys.stderr)
        sys.exit(1)

    print(hash_password(password, args.rounds))


if __name__ == "__main__":
    main()
