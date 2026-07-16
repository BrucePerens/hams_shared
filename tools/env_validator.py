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
import urllib.request
from urllib.error import URLError, HTTPError
import glob


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
            except OSError:
                pass


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
    except Exception as e:
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
            except Exception:
                pass  # Ignore if STARTTLS is not supported by the server
            try:
                server.login(user, password)
            except smtplib.SMTPAuthenticationError as e:
                print_warning("SMTP", f"Authentication failed for user '{user}': {e}")
        server.quit()
    except Exception as e:
        print_warning("SMTP", f"Failed to connect or verify SMTP server at {host}:{port}: {e}")


def check_gemini():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print_warning("GEMINI", "GEMINI_API_KEY is not set. AI features will be disabled.")
        return

    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status != 200:
                print_warning("GEMINI", f"Unexpected status code {response.status} when verifying API key.")
    except HTTPError as e:
        if e.code in (400, 401, 403):
            print_warning("GEMINI", f"API Key verification failed ({e.code}): Invalid or expired GEMINI_API_KEY.")
        else:
            print_warning("GEMINI", f"API Key verification failed with HTTP {e.code}: {e.reason}")
    except URLError as e:
        print_warning("GEMINI", f"Network error when verifying API key: {e.reason}")
    except Exception as e:
        print_warning("GEMINI", f"Unexpected error during API key verification: {e}")


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
