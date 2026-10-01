"""Polite, cached HTTP client for lrs.lt / vrk.lt.

Every response is cached to data/raw/ keyed by a hash of the URL, so re-running
a loader never re-hits the source. Rate limited and identified. Be a good
citizen: these are public services run on a public budget.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from pathlib import Path

import requests

from .config import CACHE_DIR, CONTACT_EMAIL, REQUEST_DELAY, USER_AGENT

log = logging.getLogger(__name__)

_last_request_at = 0.0


def _cache_path(url: str, suffix: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
    return CACHE_DIR / f"{digest}{suffix}"


def _get(url: str, timeout: int) -> requests.Response | None:
    """One request, honouring the delay floor whether or not the last one worked."""
    global _last_request_at

    elapsed = time.monotonic() - _last_request_at
    if elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)
    headers = {"User-Agent": f"{USER_AGENT} ({CONTACT_EMAIL})"}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        log.warning("request failed %s: %s", url, exc)
        return None
    finally:
        # a failed or timed-out request still counts against the floor
        _last_request_at = time.monotonic()
    if resp.status_code != 200:
        log.warning("HTTP %s for %s", resp.status_code, url)
        return None
    return resp


def _store(path: Path, data: bytes) -> None:
    # Write-then-rename: a harvest killed mid-write must not leave a truncated
    # response in the cache, where it would be served forever.
    part = path.with_name(path.name + ".part")
    part.write_bytes(data)
    part.replace(path)


def fetch(
    url: str,
    *,
    suffix: str = ".xml",
    force: bool = False,
    offline: bool = False,
    timeout: int = 30,
    valid: Callable[[str], bool] | None = None,
) -> str | None:
    """Fetch a URL, using the on-disk cache unless force=True.

    Returns the response body as text, or None if the request failed.
    Never raises on HTTP errors - loaders decide what a miss means.
    offline=True never touches the network: a cache miss returns None. Loaders
    use it so parsing and reloading can never become an accidental harvest.
    valid() rejects an error page served with status 200: a rejected response is
    a failure and is not cached, and a rejected cached body counts as a miss.
    """
    path = _cache_path(url, suffix)
    if path.exists() and not force:
        body = path.read_text(encoding="utf-8")
        if valid is None or valid(body):
            return body
    if offline:
        return None
    resp = _get(url, timeout)
    if resp is None:
        return None
    # lrs.lt does not always declare its encoding correctly.
    if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    body = resp.text
    if valid is not None and not valid(body):
        log.warning("error page served for %s", url)
        return None
    _store(path, body.encode("utf-8"))
    return body


def fetch_bytes(
    url: str,
    *,
    suffix: str = ".bin",
    force: bool = False,
    offline: bool = False,
    timeout: int = 60,
) -> bytes | None:
    """fetch() for binary documents such as PDFs: same cache, floor and identity."""
    path = _cache_path(url, suffix)
    if path.exists() and not force:
        return path.read_bytes()
    if offline:
        return None
    resp = _get(url, timeout)
    if resp is None:
        return None
    _store(path, resp.content)
    return resp.content


def cache_stats() -> tuple[int, int]:
    """Return (file count, total bytes) currently cached."""
    files = list(CACHE_DIR.glob("*"))
    return len(files), sum(f.stat().st_size for f in files)
