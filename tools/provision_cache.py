#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Download cache for provisioning (infrastructure.py): apt keys, the kopia release, wkhtmltopdf and the
speech models are fetched through here, so a repeated provision or test run does not hit the network
for a file it already has, and an unreachable upstream does not stop a run that has a cached copy.

Layout: <cache root>/<sha256 of the URL>/{meta.json, <sha256 of the content>.blob}. The blob is named by
its content hash, written to a temporary file and renamed, and only then does meta.json (the index)
change, also by rename, so a crash or a concurrent run never leaves a half-written entry that looks good.

Policy (docs: hams_shared/tools/README.md, "Provisioning download cache"):
  * Pinned artifact (the caller passes expected_sha256): a cached copy with that hash is used forever
    with no network. A download that does not have that hash is refused and never cached.
  * Unpinned (an apt key): trust on first use. The first observed sha256 is recorded in meta.json and
    logged. Later content with a different hash is REFUSED (HashRejected), because a changed signing key
    must be reviewed, not silently accepted. HAMS_PROVISION_REFRESH=1 (provision.py --refresh-cache)
    accepts the change and logs the old and new hash.
  * A fresh entry (younger than HAMS_PROVISION_CACHE_MAX_AGE_DAYS, default 7) is used without network.
    An older one is revalidated with If-None-Match / If-Modified-Since; a 304 renews it.
  * Stale-if-error: when the network fails and a good cached copy exists it is used, with a warning.
    With no cached copy the failure is raised (Unavailable).
  * Never cached: anything that is not https, carries credentials in the URL, or has a query parameter
    that looks like a credential. Those are fetched directly and nothing is stored.
"""

import hashlib
import json
import logging
import math
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

_logger = logging.getLogger(__name__)

CACHE_ENV = "HAMS_PROVISION_CACHE"
REFRESH_ENV = "HAMS_PROVISION_REFRESH"
MAX_AGE_ENV = "HAMS_PROVISION_CACHE_MAX_AGE_DAYS"
DEFAULT_MAX_AGE_DAYS = 7.0
ROOT_CACHE_DIR = "/var/cache/hams-provision"
DEFAULT_MAX_BYTES = 1 << 30
_CREDENTIAL_PARAMS = (
    "token", "key", "secret", "password", "passwd", "pass", "auth", "sig", "signature", "credential",
    "x-amz-", "access", "apikey", "api_key",
)


class CacheError(Exception):
    """Base class for provisioning-cache failures."""


class HashRejected(CacheError):
    """The content does not have the hash it must have (a pin mismatch, or a changed trust-on-first-use
    hash). Never retried from cache and never swallowed: the file must be reviewed by a person."""


class Unavailable(CacheError):
    """The network failed and there is no cached copy to fall back to."""


@dataclass
class Result:
    path: str        # the cached blob (read-only to the caller: copy it, do not modify or move it)
    status: str      # "hit", "revalidated", "fetched" or "stale"
    sha256: str
    cached: bool = True


def cache_root():
    """HAMS_PROVISION_CACHE, else /var/cache/hams-provision for root, else ~/.cache/hams_provision."""
    override = os.environ.get(CACHE_ENV)
    if override:
        return override
    if os.geteuid() == 0:
        return ROOT_CACHE_DIR
    return os.path.join(os.path.expanduser("~"), ".cache", "hams_provision")


def refresh_requested():
    return os.environ.get(REFRESH_ENV, "") == "1"


def max_age_seconds():
    try:
        days = float(os.environ.get(MAX_AGE_ENV, DEFAULT_MAX_AGE_DAYS))
        if not math.isfinite(days):
            raise ValueError(f"{MAX_AGE_ENV} is not a finite number of days")
    except ValueError:
        days = DEFAULT_MAX_AGE_DAYS
    return max(days, 0.0) * 86400


def is_cacheable(url):
    """True only for a plain https URL with no credentials in it."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return False
    for name, _ in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        lowered = name.lower()
        if any(word in lowered for word in _CREDENTIAL_PARAMS):
            return False
    return True


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:  # audit-ignore-path
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entry_dir(url):
    return os.path.join(cache_root(), hashlib.sha256(url.encode("utf-8")).hexdigest())


def _load_meta(entry):
    try:
        with open(os.path.join(entry, "meta.json"), encoding="utf-8") as f:  # audit-ignore-path
            meta = json.load(f)
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _write_meta(entry, meta):
    fd, tmp = tempfile.mkstemp(dir=entry, prefix=".meta-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(entry, "meta.json"))
    except BaseException:
        _unlink(tmp)
        raise


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _usable_blob(entry, meta):
    """The cached blob path when it exists and still has the hash the index recorded, else None."""
    digest = meta.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        return None
    blob = os.path.join(entry, digest + ".blob")
    try:
        if _sha256_file(blob) == digest:
            return blob
    except OSError:
        pass
    return None


def _header(headers, name):
    try:
        value = headers.get(name)
    except Exception:  # audit-ignore-catch-all
        _logger.debug("unreadable response header %s", name)
        return None
    return value if isinstance(value, str) and value else None


def _urlopen_downloader(user_agent, timeout, max_bytes):
    """The default transport: GET with conditional headers. Returns (status, headers); 304 writes nothing."""
    def download(url, tmp_path, request_headers):
        headers = {"User-Agent": user_agent}
        headers.update(request_headers)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response, open(tmp_path, "wb") as out:  # audit-ignore-path
                got = 0
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > max_bytes:
                        raise ValueError(f"{url}: more than the allowed {max_bytes} bytes")
                    out.write(chunk)
                return 200, response.headers
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, {}
            raise
    return download


def fetch(url, *, user_agent="HamsComProvisioning/1.0 (+https://hams.com)", expected_sha256=None,
          downloader=None, timeout=30, max_bytes=DEFAULT_MAX_BYTES, refresh=None, max_age=None, now=None):
    """Returns a Result for `url`, using the cache per the module docstring.

    `downloader(url, tmp_path, request_headers) -> (status, response_headers)` replaces the HTTP
    transport (tests, and provision_model_files' size-limited fetch); it must write the body to tmp_path.
    Raises HashRejected (pin mismatch or changed trust-on-first-use hash) or Unavailable (network failed,
    nothing cached). A URL that is not cacheable is downloaded to a temporary file and Result.cached is
    False: the caller must delete Result.path after use."""
    expected = expected_sha256.lower() if expected_sha256 else None
    refresh = refresh_requested() if refresh is None else refresh
    max_age = max_age_seconds() if max_age is None else max_age
    now = time.time() if now is None else now
    download = downloader or _urlopen_downloader(user_agent, timeout, max_bytes)

    if not is_cacheable(url):
        fd, tmp = tempfile.mkstemp(prefix="hams-nocache-")
        os.close(fd)
        try:
            download(url, tmp, {})
            digest = _sha256_file(tmp)
            if expected and digest != expected:
                raise HashRejected(f"{url}: sha256 {digest} is not the pinned {expected}")
        except BaseException:
            _unlink(tmp)
            raise
        return Result(tmp, "fetched", digest, cached=False)

    entry = _entry_dir(url)
    os.makedirs(entry, exist_ok=True)
    meta = _load_meta(entry)
    blob = _usable_blob(entry, meta)
    known = meta.get("sha256") if isinstance(meta.get("sha256"), str) else None

    if blob and expected and known == expected:
        return Result(blob, "hit", known)
    if blob and not expected and not refresh and now - float(meta.get("fetched_at", 0) or 0) < max_age:
        return Result(blob, "hit", known)

    conditional = {}
    if blob and not refresh and not expected:
        if meta.get("etag"):
            conditional["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            conditional["If-Modified-Since"] = meta["last_modified"]

    fd, tmp = tempfile.mkstemp(dir=entry, prefix=".dl-", suffix=".part")
    os.close(fd)
    try:
        try:
            status, headers = download(url, tmp, conditional)
        except Exception as e:  # audit-ignore-catch-all
            if blob and not (expected and known != expected):
                _logger.warning("Could not fetch %s (%s); using the cached copy (sha256 %s)", url, e, known)
                return Result(blob, "stale", known)
            raise Unavailable(f"{url} could not be fetched and no cached copy exists: {e}") from e

        if status == 304 and blob:
            meta["fetched_at"] = now
            _write_meta(entry, meta)
            return Result(blob, "revalidated", known)

        digest = _sha256_file(tmp)
        if expected and digest != expected:
            raise HashRejected(f"{url}: downloaded sha256 {digest} is not the pinned {expected}; not cached")
        if not expected and known and digest != known:
            if not refresh:
                raise HashRejected(
                    f"{url}: content changed. Recorded sha256 {known}, now {digest}. A changed key or file must be "
                    f"reviewed, not silently accepted. After reviewing it, re-run with --refresh-cache "
                    f"({REFRESH_ENV}=1) to accept the new content."
                )
            _logger.warning("Accepting changed content for %s under %s=1: sha256 %s -> %s", url, REFRESH_ENV, known, digest)
        elif not expected and not known:
            _logger.warning("Trust on first use: %s has sha256 %s (recorded in %s)", url, digest, entry)

        new_blob = os.path.join(entry, digest + ".blob")
        os.replace(tmp, new_blob)
        os.chmod(new_blob, 0o644)
        _write_meta(entry, {
            "url": url, "sha256": digest, "size": os.path.getsize(new_blob), "fetched_at": now,
            "etag": _header(headers, "ETag"), "last_modified": _header(headers, "Last-Modified"),
            "pinned": bool(expected),
        })
        for name in os.listdir(entry):
            if name.endswith(".blob") and name != digest + ".blob":
                _unlink(os.path.join(entry, name))
        return Result(new_blob, "fetched", digest)
    finally:
        _unlink(tmp)


def install(result, dest, mode):
    """Copies the fetched file to `dest` atomically (temporary file in the same directory, then rename), so a
    reader never sees a partial file and a failure leaves any existing `dest` untouched. Removes the source
    when it was not cached."""
    directory = os.path.dirname(dest) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".install-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out, open(result.path, "rb") as src:  # audit-ignore-path
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
            out.flush()
            os.fchmod(out.fileno(), mode)
        os.replace(tmp, dest)
    except BaseException:
        _unlink(tmp)
        raise
    finally:
        if not result.cached:
            _unlink(result.path)
