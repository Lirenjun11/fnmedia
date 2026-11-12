#!/usr/bin/env python3
"""豆瓣真实榜单缓存（虚拟媒体库的“展示层”）。

设计要点（对齐 LibreTV 的两层结构）：
- 展示层：后台拉取豆瓣周榜（经 60s 公开 API 免 key 转发）+ 豆瓣 tag 推荐，
  合成 4 个虚拟库（电影 / 剧集 / 动漫 / 综艺），只提供片名、海报、评分、
  年份、简介等元数据；原子写入 state/charts.json，媒体库只读、秒开。
- 播放层：榜单条目的 guid 稳定（md5("fnmedia-chart:<cat>:<douban_id>")），
  点进详情时才用片名实时聚合搜索资源站，命中后把可播放 brief 挂到该 guid
  （见 vlib.py），复用既有的详情 / 选集 / 线路 / 续播 / 代理播放链。
- 容错：任一配方失败不影响其他；整体失败或结果为空时保留旧缓存。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import time
from pathlib import Path

import httpx

import config

STATE = config.STATE_DIR / "charts.json"
REFRESH_TIMEOUT = 90.0

# 60s API：社区实例优先，官方公共实例兜底（数据中心 IP 常被 Cloudflare 拦）。
SIXTY_BASES = ("https://60s.crystelf.top", "https://60s.viki.moe")
DOUBAN_SUBJECTS = "https://movie.douban.com/j/search_subjects"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# cat -> (媒体库展示名, fnOS MediaDB category)
CHART_CATEGORIES = [
    ("movie", "🎬热门电影", "Movie"),
    ("tv", "📺热门剧集", "TV"),
    ("anime", "🌸动漫新番", "Mix"),
    ("variety", "🎤热门综艺", "Mix"),
]

# 每个库的取数配方：weekly=60s 豆瓣周榜；tag=豆瓣官方 search_subjects（可翻页）。
_CHART_RECIPES: dict[str, list[dict]] = {
    "movie": [
        {"kind": "weekly", "path": "movie"},
        {"kind": "tag", "type": "movie", "tag": "热门", "pages": 4},
        {"kind": "tag", "type": "movie", "tag": "最新", "pages": 3},
        {"kind": "tag", "type": "movie", "tag": "豆瓣高分", "pages": 2},
        {"kind": "tag", "type": "movie", "tag": "华语", "pages": 2},
        {"kind": "tag", "type": "movie", "tag": "欧美", "pages": 2},
    ],
    "tv": [
        {"kind": "weekly", "path": "tv_chinese"},
        {"kind": "weekly", "path": "tv_global"},
        {"kind": "tag", "type": "tv", "tag": "热门", "pages": 4},
        {"kind": "tag", "type": "tv", "tag": "美剧", "pages": 3},
        {"kind": "tag", "type": "tv", "tag": "国产剧", "pages": 3},
        {"kind": "tag", "type": "tv", "tag": "韩剧", "pages": 2},
        {"kind": "tag", "type": "tv", "tag": "日剧", "pages": 2},
    ],
    "anime": [
        {"kind": "tag", "type": "tv", "tag": "日本动画", "pages": 5},
        {"kind": "tag", "type": "tv", "tag": "动画", "pages": 4},
        {"kind": "tag", "type": "movie", "tag": "动画", "pages": 3},
    ],
    "variety": [
        {"kind": "weekly", "path": "show_chinese"},
        {"kind": "weekly", "path": "show_global"},
        {"kind": "tag", "type": "tv", "tag": "综艺", "pages": 6},
        {"kind": "tag", "type": "tv", "tag": "真人秀", "pages": 3},
        {"kind": "tag", "type": "tv", "tag": "脱口秀", "pages": 2},
    ],
}

CHART_LIMIT = 160
TAG_PAGE_SIZE = 20
_TAG_CONCURRENCY = 4


def chart_guid(cat: str, douban_id: str) -> str:
    return hashlib.md5(f"fnmedia-chart:{cat}:{douban_id}".encode()).hexdigest()


def chart_cat_title(cat: str) -> str:
    for key, title, _ in CHART_CATEGORIES:
        if key == cat:
            return title
    return "🌐在线热门"


def chart_cat_mdb(cat: str) -> str:
    for key, _, mdb in CHART_CATEGORIES:
        if key == cat:
            return mdb
    return "Mix"


def _to_float(value) -> float:
    try:
        number = float(value)
        return number if number > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _year_of(text: str) -> str:
    match = re.search(r"(19|20)\d{2}", text or "")
    return match.group(0) if match else ""


def _normalize(raw: dict, cat: str, origin: str) -> dict | None:
    douban_id = str(raw.get("id") or "")
    title = (raw.get("title") or "").strip()
    cover = raw.get("cover") or raw.get("cover_proxy") or ""
    if not douban_id or not title or not cover:
        return None
    subtitle = (raw.get("card_subtitle") or raw.get("subtitle") or "").strip()
    return {
        "id": douban_id,
        "guid": chart_guid(cat, douban_id),
        "cat": cat,
        "title": title,
        "rating": _to_float(raw.get("rating")),
        "cover": cover,
        "year": _year_of(subtitle),
        "subtitle": subtitle,
        "desc": (raw.get("description") or "").strip(),
        "tags": raw.get("tags") or [],
        "origin": origin,
    }


async def _fetch_weekly(client: httpx.AsyncClient, path: str) -> list[dict]:
    last = None
    for base in SIXTY_BASES:
        try:
            resp = await client.get(
                f"{base}/v2/douban/weekly/{path}",
                headers={"User-Agent": _UA, "Accept": "application/json"},
                timeout=12)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            payload = resp.json()
            rows = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(rows, list) and rows:
                return rows
        except Exception as exc:  # noqa: BLE001 - 逐个实例降级
            last = exc
    if last:
        raise last
    return []


async def _fetch_tag(client: httpx.AsyncClient, typ: str, tag: str,
                     pages: int) -> list[dict]:
    out: list[dict] = []
    for page in range(max(1, int(pages))):
        try:
            resp = await client.get(
                DOUBAN_SUBJECTS,
                params={"type": typ, "tag": tag, "sort": "recommend",
                        "page_limit": TAG_PAGE_SIZE,
                        "page_start": page * TAG_PAGE_SIZE},
                headers={"User-Agent": _UA,
                         "Referer": "https://movie.douban.com/",
                         "Accept": "application/json"},
                timeout=10)
            if resp.status_code != 200:
                break
            subjects = (resp.json() or {}).get("subjects") or []
        except Exception:  # noqa: BLE001 - 单页失败即停，不影响其他配方
            break
        if not subjects:
            break
        for row in subjects:
            out.append({
                "id": row.get("id"), "title": row.get("title"),
                "rating": row.get("rate"), "cover": row.get("cover"),
                "card_subtitle": row.get("episodes_info") or "",
                "_origin": "douban-tag", "_tag": tag,
            })
    return out


async def collect(client: httpx.AsyncClient) -> dict:
    """并发拉取全部配方，去重合并为 4 个库。"""
    sem = asyncio.Semaphore(8)
    tag_sem = asyncio.Semaphore(_TAG_CONCURRENCY)
    jobs: list[tuple[str, dict, asyncio.Task]] = []
    provider_total = 0
    for cat, recipes in _CHART_RECIPES.items():
        for recipe in recipes:
            provider_total += 1

            async def run(rc=recipe):
                semaphore = tag_sem if rc["kind"] == "tag" else sem
                async with semaphore:
                    if rc["kind"] == "weekly":
                        return await _fetch_weekly(client, rc["path"])
                    return await _fetch_tag(client, rc["type"], rc["tag"],
                                            int(rc.get("pages", 1)))

            jobs.append((cat, recipe, asyncio.ensure_future(run())))

    await asyncio.gather(*[job[2] for job in jobs], return_exceptions=True)

    raw: dict[str, list[dict]] = {cat: [] for cat, _, _ in CHART_CATEGORIES}
    provider_ok = 0
    for cat, recipe, task in jobs:
        if task.cancelled() or isinstance(task.exception(), Exception):
            continue
        provider_ok += 1
        origin = "douban-weekly" if recipe["kind"] == "weekly" else "douban-tag"
        for row in task.result() or []:
            if not isinstance(row, dict):
                continue
            if recipe["kind"] == "tag":
                row = {**row, "tag": recipe["tag"]}
            item = _normalize(row, cat, origin)
            if item:
                raw[cat].append(item)

    charts: dict[str, list[dict]] = {}
    for cat, _, _ in CHART_CATEGORIES:
        seen: set[str] = set()
        merged: list[dict] = []
        for item in raw.get(cat, []):
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            merged.append(item)
        charts[cat] = merged[:CHART_LIMIT]

    return {
        "updated_at": int(time.time()),
        "provider_ok": provider_ok,
        "provider_total": provider_total,
        "charts": charts,
    }


class ChartCache:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._charts: dict[str, list[dict]] = {c: [] for c, _, _ in CHART_CATEGORIES}
        self._index: dict[str, dict] = {}
        self._meta = {"updated_at": 0, "provider_ok": 0, "provider_total": 0,
                      "last_error": "尚未刷新"}
        self._refreshing = False
        self.load()

    # ---------- 持久化 ----------
    def load(self) -> None:
        with self._lock:
            try:
                data = json.loads(STATE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            if isinstance(data, dict) and isinstance(data.get("charts"), dict):
                self._apply(data)
            else:
                self._charts = {c: [] for c, _, _ in CHART_CATEGORIES}
                self._index = {}
                self._meta["last_error"] = "尚未刷新"

    def _apply(self, data: dict) -> None:
        self._charts = {cat: list((data.get("charts") or {}).get(cat) or [])
                        for cat, _, _ in CHART_CATEGORIES}
        self._index = {item["guid"]: item for rows in self._charts.values()
                       for item in rows if item.get("guid")}
        self._meta = {
            "updated_at": int(data.get("updated_at") or 0),
            "provider_ok": int(data.get("provider_ok") or 0),
            "provider_total": int(data.get("provider_total") or 0),
            "last_error": str(data.get("last_error") or ""),
        }

    def _persist(self, data: dict) -> None:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False,
                                  separators=(",", ":")), encoding="utf-8")
        tmp.replace(STATE)

    # ---------- 读取 ----------
    def items(self, cat: str) -> list[dict]:
        with self._lock:
            return list(self._charts.get(cat, ()))

    def all_items(self) -> list[dict]:
        with self._lock:
            return [item for rows in self._charts.values() for item in rows]

    def get(self, guid: str) -> dict | None:
        with self._lock:
            return self._index.get(guid)

    def is_chart_guid(self, guid: str) -> bool:
        with self._lock:
            return guid in self._index

    def covers(self, cat: str, limit: int = 8) -> list[str]:
        out = []
        for item in self.items(cat):
            cover = item.get("cover") or ""
            if cover:
                out.append(cover)
            if len(out) >= limit:
                break
        return out

    def status(self) -> dict:
        with self._lock:
            return {
                "refreshing": self._refreshing,
                **self._meta,
                "counts": {cat: len(self._charts.get(cat, ()))
                           for cat, _, _ in CHART_CATEGORIES},
            }

    def total(self) -> int:
        with self._lock:
            return sum(len(rows) for rows in self._charts.values())

    def stale(self, interval: float) -> bool:
        with self._lock:
            updated = float(self._meta.get("updated_at") or 0)
        return not updated or (time.time() - updated) >= interval

    def reset(self) -> None:
        with self._lock:
            self._charts = {c: [] for c, _, _ in CHART_CATEGORIES}
            self._index = {}
            self._meta = {"updated_at": 0, "provider_ok": 0,
                          "provider_total": 0, "last_error": "已手动清空"}
            try:
                STATE.unlink()
            except OSError:
                pass

    # ---------- 刷新 ----------
    async def refresh(self, client: httpx.AsyncClient) -> dict:
        if self._refreshing:
            return self.status()
        self._refreshing = True
        try:
            data = await asyncio.wait_for(collect(client), timeout=REFRESH_TIMEOUT)
            total = sum(len(rows) for rows in (data.get("charts") or {}).values())
            if data.get("provider_ok", 0) == 0 or total == 0:
                raise RuntimeError("榜单全部获取失败或为空，保留旧缓存")
            data["last_error"] = ""
            with self._lock:
                self._persist(data)
                self._apply(data)
        except Exception as exc:  # noqa: BLE001 - 刷新失败不能影响线上列表
            with self._lock:
                self._meta["last_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        finally:
            self._refreshing = False
        return self.status()


chart_cache = ChartCache()
