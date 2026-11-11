"""Douban metadata enrichment (optional, disabled by default).

Queries the Douban subject-suggest endpoint only for the title the user
opened, and upgrades the portrait poster to the large variant. Falls back
silently and never provides any media source.
"""
from __future__ import annotations

import re
import threading
import time
import unicodedata

import httpx

_SUGGEST = "https://movie.douban.com/j/subject_suggest"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://movie.douban.com/",
}
_PUNCT_RE = re.compile(r"[\s·・：:，,。.．\-—_（）()\[\]【】'\"！!?？~～]+")
_LOCK = threading.Lock()
_CACHE = {}
_OK_TTL = 86400.0
_FAIL_TTL = 600.0
_TIMEOUT = 2.0


def _norm(text) -> str:
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    return _PUNCT_RE.sub("", s)


def _large(img: str) -> str:
    return str(img or "").replace("/s_ratio_poster/", "/l_ratio_poster/")


def _year_of(entry: dict):
    value = str(entry.get("year") or "")
    return int(value[:4]) if value[:4].isdigit() else None


async def enrich_poster(client: httpx.AsyncClient, detail: dict, title, year) -> dict:
    keyword = _norm(title) + "|" + str(year or "")
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(keyword)
        if hit and now - hit[0] < (_OK_TTL if hit[1] else _FAIL_TTL):
            cached = hit[1]
            if cached:
                detail["backdrops"] = cached
                detail["posters"] = cached
            return detail
    matched = None
    try:
        resp = await client.get(_SUGGEST, params={"q": str(title or "")[:60]},
                                headers=_HEADERS, timeout=_TIMEOUT)
        if resp.status_code == 200:
            try:
                year_num = int(str(year or "")[:4])
            except (TypeError, ValueError):
                year_num = None
            for entry in resp.json():
                if not isinstance(entry, dict):
                    continue
                if _norm(entry.get("title")) != _norm(title):
                    continue
                official_year = _year_of(entry)
                if year_num and official_year and abs(official_year - year_num) > 1:
                    continue
                matched = entry
                break
    except Exception:
        matched = None
    poster = _large(matched.get("img")) if matched else ""
    with _LOCK:
        _CACHE[keyword] = (now, poster)
    if poster:
        detail["backdrops"] = poster
        detail["posters"] = poster
    return detail


def cache_size() -> int:
    return len(_CACHE)
