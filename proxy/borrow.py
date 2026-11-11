"""官方飞牛影视数据库只读借用（mode=ro，不写不锁）。

按「规范标题 + 年份±1」匹配本地库条目，命中后把刮削好的横版背景图、
海报、logos、简介、评分、时长、IMDb ID、官方分类 ID 与带头像演职人员
借给在线条目；未命中静默回退 CMS 数据。
"""
from __future__ import annotations

import difflib
import os
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import closing

CANDIDATES = [
    "/usr/local/apps/@appdata/trim.media/database",
    "/vol2/@appdata/trim.media/database",
    "/vol1/@appdata/trim.media/database",
]
_PUNCT_RE = re.compile(r"[\s·・：:，,。.．\-—_（）()\[\]【】'\"！!?？~～]+")
_lock = threading.Lock()
_CACHE_TTL = 30.0
_cache: dict = {}
_items_cache: tuple | None = None

DETAIL_FIELDS = ("imdb_id", "tmdb_id", "original_title", "overview", "runtime",
                 "vote_average", "vote_count", "backdrops", "backdrop_width",
                 "backdrop_height", "logos", "posters", "poster_width",
                 "poster_height", "production_countries", "number_of_episodes",
                 "number_of_seasons", "air_date")


def _norm(text) -> str:
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    return _PUNCT_RE.sub("", s)


def _db_dir() -> str | None:
    for d in CANDIDATES:
        if os.path.isfile(os.path.join(d, "trimmedia.db")):
            return d
    return None


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=0", uri=True,
                           timeout=2.0, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _year_of(row) -> int | None:
    for key in ("release_date", "air_date", "first_air_date"):
        value = str(row[key] or "")
        if value[:4].isdigit():
            return int(value[:4])
    return None


def _items() -> list:
    global _items_cache
    now = time.monotonic()
    if _items_cache is not None:
        ts, rows = _items_cache
        if now - ts < _CACHE_TTL:
            return rows
    rows: list = []
    d = _db_dir()
    if d:
        try:
            with closing(_connect(os.path.join(d, "trimmedia.db"))) as conn:
                rows = conn.execute(
                    "SELECT guid, title, original_title, release_date, air_date, "
                    "first_air_date, imdb_id, tmdb_id, overview, runtime, "
                    "vote_average, vote_count, backdrops, backdrop_width, "
                    "backdrop_height, logos, posters, poster_width, poster_height, "
                    "production_countries, number_of_episodes, number_of_seasons "
                    "FROM item WHERE type <> 'MediaDB' AND parent_guid = '' "
                    "AND title <> ''").fetchall()
        except sqlite3.Error:
            rows = []
    _items_cache = (now, rows)
    return rows


def match_official(title, year):
    keyword = _norm(title) + "|" + str(year or "")
    now = time.monotonic()
    with _lock:
        hit = _cache.get(keyword)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
    try:
        year_num = int(str(year or "")[:4])
    except (TypeError, ValueError):
        year_num = None
    target = _norm(title)
    row = None
    for item in _items():
        name = _norm(item["title"]) or _norm(item["original_title"])
        if not name or name != target:
            continue
        official_year = _year_of(item)
        if year_num and official_year and abs(official_year - year_num) > 1:
            continue
        row = item
        break
    if row is None and len(target) >= 4:
        for item in _items():
            name = _norm(item["title"]) or _norm(item["original_title"])
            if not name or abs(len(name) - len(target)) > 2:
                continue
            if difflib.SequenceMatcher(None, name, target).ratio() < 0.8:
                continue
            official_year = _year_of(item)
            if year_num and official_year and abs(official_year - year_num) > 1:
                continue
            row = item
            break
    with _lock:
        _cache[keyword] = (now, row)
    return row


def _genre_ids(conn: sqlite3.Connection, guid: str) -> list[int]:
    out = []
    for row in conn.execute(
            "SELECT tag FROM item_tag WHERE item_guid = ? AND type = 'Genre'",
            (guid,)):
        try:
            out.append(int(row[0]))
        except (TypeError, ValueError):
            continue
    return out


def enrich_detail(detail: dict, title, year) -> dict:
    row = match_official(title, year)
    if row is None:
        return detail
    detail["_borrowed"] = True
    for key in DETAIL_FIELDS:
        value = row[key]
        if value in (None, ""):
            continue
        detail[key] = value
    detail["vote_average"] = str(detail["vote_average"] or "0")
    d = _db_dir()
    if d:
        try:
            with closing(_connect(os.path.join(d, "trimmedia.db"))) as conn:
                ids = _genre_ids(conn, row["guid"])
            if ids:
                detail["genres"] = ids
        except (sqlite3.Error, IndexError, KeyError):
            pass
    return detail


def person_rows(title, year) -> list | None:
    row = match_official(title, year)
    if row is None:
        return None
    d = _db_dir()
    if not d:
        return None
    out: list = []
    try:
        with closing(_connect(os.path.join(d, "trimmedia.db"))) as conn:
            for r in conn.execute(
                    "SELECT p.*, ip.role AS prole, ip.job AS pjob, "
                    "ip.\"order\" AS ord, ip.department AS pdep "
                    "FROM item_person ip JOIN person p ON p.guid = ip.person_guid "
                    "WHERE ip.item_guid = ? ORDER BY ip.\"order\"",
                    (row["guid"],)):
                out.append({
                    "item_guid": row["guid"], "person_guid": r["guid"],
                    "role": r["prole"] or "", "job": r["pjob"] or "",
                    "order": r["ord"] or 0, "department": r["pdep"] or "",
                    "trim_id": r["trim_id"] or "", "imdb_id": r["imdb_id"] or "",
                    "tmdb_id": r["tmdb_id"] or 0, "lan": r["lan"] or "zh-CN",
                    "name": r["name"] or "",
                    "original_name": r["original_name"] or "",
                    "also_know_as": r["also_know_as"] or "",
                    "biography": r["biography"] or "",
                    "known_for_department": r["know_for_department"] or "",
                    "profile_path": r["profile_path"] or "", "gender": r["gender"] or 0,
                })
    except sqlite3.Error:
        return None
    return out
