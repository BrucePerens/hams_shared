#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Derives an Amazon SES SMTP password from an IAM secret access key.

SES's SMTP interface authenticates with an IAM access key id (as SMTP_USER) and a password
derived from the matching IAM *secret* access key via AWS's own published algorithm -- a fixed
SigV4-style HMAC-SHA256 chain over the literal string "SendRawEmail", not the secret key itself
sent as-is. AWS shows the secret access key only once, at creation time; this derivation can be
re-run any time afterward from a securely stored copy of that same secret, without needing to
regenerate the key.

# [@ANCHOR: derive_ses_smtp_password]
# Verified by [@ANCHOR: test_derive_ses_smtp_password_matches_the_documented_test_vector]

Usage, reading the secret from stdin so it never appears in a shell history or process list
(`ps` shows argv, not stdin):

    echo -n "$SECRET_ACCESS_KEY" | python3 derive_ses_smtp_password.py --region us-east-1

Writes only the derived password to stdout -- never the secret key itself.
"""
import argparse
import base64
import hashlib
import hmac
import sys

_DATE = "11111111"
_SERVICE = "ses"
_MESSAGE = "SendRawEmail"
_TERMINAL = "aws4_request"
_VERSION = 0x04


def _sign(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def calculate_smtp_password(secret_access_key, region):
    """The SES SMTP password for `secret_access_key` in `region`, per AWS's documented
    conversion algorithm (a fixed HMAC-SHA256 chain, not literal SigV4 request signing)."""
    signature = _sign(("AWS4" + secret_access_key).encode("utf-8"), _DATE)
    signature = _sign(signature, region)
    signature = _sign(signature, _SERVICE)
    signature = _sign(signature, _TERMINAL)
    signature = _sign(signature, _MESSAGE)
    signature_and_version = bytes([_VERSION]) + signature
    return base64.b64encode(signature_and_version).decode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--region", required=True, help="AWS region the IAM access key's SES SMTP endpoint is in."
    )
    args = parser.parse_args()
    secret_access_key = sys.stdin.readline().strip()
    if not secret_access_key:
        parser.error("no secret access key on stdin")
    print(calculate_smtp_password(secret_access_key, args.region))


if __name__ == "__main__":
    main()
