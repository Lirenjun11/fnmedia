"""Read-only update check against GitHub releases/latest."""
from __future__ import annotations

import re
import threading
import time

import httpx

_API = "https://api.github.com/repos/{repo}/releases/latest"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_DEFAULT_REPO = "Lirenjun11/fnmedia"
_OK_TTL = 21600.0
_FAIL_TTL = 600.0
_TIMEOUT = 5.0
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "fnos_media_ext-update-check",
}
_lock = threading.Lock()
_cache: dict = {}


def _parse(tag) -> tuple | None:
    match = _TAG_RE.match(str(tag or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def is_newer(current, latest) -> bool:
    cur = _parse(current)
    new = _parse(latest)
    if not new:
        return False
    return new > cur if cur else True


def safe_repo(value) -> str:
    repo = str(value or "").strip()
    if not _REPO_RE.match(repo):
        return _DEFAULT_REPO
    owner, name = repo.split("/", 1)
    if ".." in repo or owner.startswith((".", "-")) or name.startswith((".", "-")):
        return _DEFAULT_REPO
    return repo


def _empty(current: str) -> dict:
    return {"current": current, "latest": "", "has_update": False,
            "html_url": "", "notes": "", "assets": [], "published_at": "",
            "error": "", "cached": False}


async def _fetch(client: httpx.AsyncClient, repo: str, current: str) -> dict:
    payload = _empty(current)
    try:
        resp = await client.get(_API.format(repo=repo), headers=_HEADERS,
                                timeout=_TIMEOUT)
        if resp.status_code == 404:
            payload["error"] = "no_release"
            return payload
        resp.raise_for_status()
        data = resp.json()
        tag = str(data.get("tag_name") or "")
        if data.get("draft") or data.get("prerelease") or not _parse(tag):
            payload["error"] = "unstable"
            return payload
        payload["latest"] = tag.lstrip("v")
        payload["has_update"] = is_newer(current, tag)
        if not payload["has_update"] and is_newer(tag, current):
            payload["latest"] = str(current).lstrip("v")
        payload["html_url"] = str(data.get("html_url") or "")
        payload["notes"] = str(data.get("body") or "")[:1000]
        payload["published_at"] = str(data.get("published_at") or "")
        payload["assets"] = [
            {"name": str(asset.get("name") or ""),
             "url": str(asset.get("browser_download_url") or "")}
            for asset in (data.get("assets") or [])[:6]]
    except Exception:
        payload = _empty(current)
        payload["error"] = "unavailable"
    return payload


async def check(repo, current: str, force: bool = False,
                client: httpx.AsyncClient | None = None) -> dict:
    repo = safe_repo(repo)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(repo)
        if hit and not force:
            ts, ok, payload = hit
            if now - ts < (_OK_TTL if ok else _FAIL_TTL):
                return {**payload, "current": current, "cached": True}
    if client is not None:
        payload = await _fetch(client, repo, current)
    else:
        async with httpx.AsyncClient(timeout=_TIMEOUT, trust_env=False,
                                    follow_redirects=True) as owned:
            payload = await _fetch(owned, repo, current)
    with _lock:
        _cache[repo] = (now, not payload["error"], payload)
    return payload
