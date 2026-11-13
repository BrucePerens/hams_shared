# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backup passphrases and keys: asked for on the terminal, kept in memory, never written anywhere.

`ask` opens /dev/tty itself and fails when there is none: a passphrase is never taken from standard input, an argument,
an environment variable or a file, so none lands in shell history, a log or a job record. `Secrets.env_for` hands
a secret to ONE child program through that program's own environment.
"""
import getpass
import os

from .runner import RestorationError

KOPIA = "kopia_password"
PGBACKREST = "pgbackrest_cipher_passphrase"
B2_KEY_ID = "b2_application_key_id"
B2_KEY = "b2_application_key"

LABELS = {
    KOPIA: "kopia repository password (custody sheet: kopia_repo_password)",
    PGBACKREST: "pgBackRest repository passphrase (custody sheet: pgbackrest_repo2_cipher_pass)",
    B2_KEY_ID: "Backblaze B2 application key id",
    B2_KEY: "Backblaze B2 application key",
}


def ask(label):
    """The value typed at the terminal, with no echo. RestorationError when there is no terminal or the answer is empty."""
    try:
        os.close(os.open("/dev/tty", os.O_RDWR))
    except OSError as exc:
        raise RestorationError(
            "no terminal (/dev/tty): the backup passphrases are typed at a terminal, never taken from arguments, "
            "environment variables or files. Run this command from an interactive shell. (%s)" % exc)
    value = getpass.getpass("%s: " % label)
    if not value:
        raise RestorationError("empty answer for %s" % label)
    return value


# [@ANCHOR: secrets_input:secrets_input_secrets_class]
class Secrets:
    """In-memory store. Nothing here is ever printed: __repr__ shows names only."""

    def __init__(self, values=None):
        self._values = dict(values or {})

    def __repr__(self):
        return "Secrets(%s)" % ",".join(sorted(self._values))

    def need(self, names, asker=ask):
        for name in names:
            if name not in self._values:
                self._values[name] = asker(LABELS[name])

    def get(self, name):
        return self._values[name]

    def has(self, name):
        return name in self._values

    def kopia_env(self):
        env = {"KOPIA_PASSWORD": self._values[KOPIA], "KOPIA_CHECK_FOR_UPDATES": "false"}
        if B2_KEY in self._values:
            env["AWS_ACCESS_KEY_ID"] = self._values[B2_KEY_ID]
            env["AWS_SECRET_ACCESS_KEY"] = self._values[B2_KEY]
        return env

    def pgbackrest_env(self):
        env = {"PGBACKREST_REPO1_CIPHER_PASS": self._values[PGBACKREST]}
        if B2_KEY in self._values:
            env["PGBACKREST_REPO1_S3_KEY"] = self._values[B2_KEY_ID]
            env["PGBACKREST_REPO1_S3_KEY_SECRET"] = self._values[B2_KEY]
        return env
