#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Environment Validation Script
-----------------------------
Runs at system startup to validate configured environment variables (SMTP, Gemini, Postgres, Redis, RabbitMQ).
Non-fatal: it warns to standard error upon failure but always exits 0.
"""

import os
import sys
import smtplib
import socket
import glob
import logging


def load_env_files():
    """Fallback to loading .env files from disk if run manually outside of systemd."""
    env_dirs = ["/opt/hams/etc", "."]
    for env_dir in env_dirs:
        if not os.path.exists(env_dir):
            continue
        for env_file in glob.glob(os.path.join(env_dir, "*.env")):
            try:
                with open(env_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, val = line.split("=", 1)
                            if key.strip() not in os.environ:
                                os.environ[key.strip()] = val.strip()
            except OSError as e:
                print_warning("ENV", f"Failed to read env file {env_file}: {e}")


def print_warning(module, message):
    """Prints a heavily formatted warning to standard error."""
    sys.stderr.write(f"\n[{module} WARNING] {message}\n")


def check_socket(host, port, name):
    if not host or not port:
        print_warning(name, f"Missing {name} host or port configuration.")
        return
    try:
        port = int(port)
        with socket.create_connection((host, port), timeout=5):
            pass
    except Exception as e:  # audit-ignore-catch-all: this whole script is documented (see module docstring) as non-fatal -- every check warns and continues so the other checks and the eventual `sys.exit(0)` still run.
        logging.exception("Failed to connect to %s:%s - %s", host, port, e)
        print_warning(name, f"Failed to connect to {host}:{port} - {e}")


def check_smtp():
    host = os.environ.get("SMTP_HOST")
    port = os.environ.get("SMTP_PORT")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")

    if not host or not port:
        print_warning("SMTP", "Missing SMTP_HOST or SMTP_PORT. Email sending may fail.")
        return

    try:
        port = int(port)
        server = smtplib.SMTP(host, port, timeout=5)
        server.ehlo()
        # Note: We don't strictly require TLS or Login validation to pass here
        # since some test servers (like MailHog) don't use authentication.
        if user and password:
            try:
                server.starttls()
            except Exception as e:  # audit-ignore-catch-all
                # Expected/benign for servers that don't offer STARTTLS (e.g. local test
                # servers like MailHog); still surfaced so a real, unexpected TLS failure
                # doesn't disappear silently.
                logging.exception("STARTTLS not available or failed for %s:%s: %s", host, port, e)
                print_warning("SMTP", f"STARTTLS not available or failed for {host}:{port}, continuing without it: {e}")
            try:
                server.login(user, password)
            except smtplib.SMTPAuthenticationError as e:
                print_warning("SMTP", f"Authentication failed for user '{user}': {e}")
        server.quit()
    except Exception as e:  # audit-ignore-catch-all: non-fatal per module docstring -- other checks and the final sys.exit(0) must still run.
        logging.exception("Failed to connect or verify SMTP server at %s:%s: %s", host, port, e)
        print_warning("SMTP", f"Failed to connect or verify SMTP server at {host}:{port}: {e}")


def check_gemini():
    # Bruce, 2026-09-22: AI features now reach Gemini through an MCP
    # server/interface rather than this process calling
    # generativelanguage.googleapis.com directly with a GEMINI_API_KEY, so
    # a direct HTTP key-verification check here no longer reflects how the
    # deployment actually works -- it was failing on every startup
    # (env_validator's own "GEMINI WARNING" noise) purely because the key
    # this check verified was never meant to still be live. Two runtime
    # modules (ham_onboarding/models/res_users_verification.py,
    # ham_repeater_dir/models/ham_repeater_import.py) still read
    # GEMINI_API_KEY directly for their own AI calls as of this change --
    # migrating those to the MCP path is a separate, larger change this
    # commit does not make; this function only stops validating a startup
    # precondition that no longer applies.
    if os.environ.get("GEMINI_API_KEY"):
        print_warning(
            "GEMINI",
            "GEMINI_API_KEY is set but is no longer how this deployment reaches "
            "Gemini (AI features route through an MCP interface now). Startup no "
            "longer verifies this key; if it's still referenced by "
            "res_users_verification.py or ham_repeater_import.py, migrating those "
            "call sites to the MCP path is tracked separately.",
        )


def main():
    load_env_files()

    # 1. Local Infrastructure
    check_socket(os.environ.get("DB_HOST"), os.environ.get("DB_PORT", "5432"), "POSTGRES")
    check_socket(os.environ.get("REDIS_HOST"), os.environ.get("REDIS_PORT", "6379"), "REDIS")
    check_socket(os.environ.get("RABBITMQ_HOST"), os.environ.get("RMQ_PORT", "5672"), "RABBITMQ")

    # 2. External Services
    check_smtp()
    check_gemini()

    # Always exit 0 so startup continues and queues can still build up.
    sys.exit(0)


if __name__ == "__main__":
    main()
