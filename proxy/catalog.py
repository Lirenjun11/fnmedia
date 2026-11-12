#!/usr/bin/env python3
"""资源站海量分类片库（虚拟媒体库的“内容层”）。

思路（对齐参考项目的“方案 C”）：
- 启动后/每 6 小时读取各资源站苹果 CMS 分类表 ``ac=list``，把全部子分类按
  名称归并到 电影 / 剧集 / 动漫 / 综艺 四大类，剔除解说、预告、短剧、福利
  等脏类；
- 每类探测若干候选子类的第一页，选“总页数最多”的源作为规范源（其余源天然
  作为回退：某页失败自动换下一个候选）；
- 浏览时按页直连 ``ac=detail&t=<子类id>&pg=N``（详情自带播放地址，点开即可
  播，无需再搜索）；页级 10 分钟内存缓存 + 单飞，避免翻页打爆资源站。

分类路由表只持久化“哪个源、哪个子类、多少页”，不落库任何片名/线路。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx

import cms
import config

STATE = config.STATE_DIR / "catalog.json"
PER_PAGE = 20
PAGE_TTL = 600.0
ROUTE_TTL = 6 * 3600.0
_PROBE_TIMEOUT = 6.0

CATALOG_CATEGORIES = [
    ("movie", "🎞海量电影", "Movie"),
    ("tv", "📺海量剧集", "TV"),
    ("anime", "🌸海量动漫", "Mix"),
    ("variety", "🎤海量综艺", "Mix"),
]

# 脏类：命中即不参与分类归并（解说/预告/福利/短剧等）。
_JUNK_WORDS = ("伦理", "福利", "写真", "情色", "成人", "三级", "里番",
               "解说", "预告", "花絮", "短剧", "体育", "赛事", "直播",
               "游戏", "记录片解说", "综艺解说", "动漫解说", "电影解说")

_ANIME_WORDS = ("动漫", "动画", "番剧", "国漫", "日漫", "里番")
_VARIETY_WORDS = ("综艺", "真人秀", "脱口秀", "晚会", "访谈")
_TV_WORDS = ("电视剧", "连续剧", "剧集", "美剧", "韩剧", "日剧", "国产剧",
             "欧美剧", "港剧", "台剧", "泰剧", "海外剧", "短剧")
_MOVIE_WORDS = ("电影", "影片", "片", "纪录片")


def catalog_key(cat: str) -> str:
    return f"catalog:{cat}"


def catalog_title(cat: str) -> str:
    for key, title, _ in CATALOG_CATEGORIES:
        if key == cat:
            return title
    return "🌐海量片库"


def catalog_mdb(cat: str) -> str:
    for key, _, mdb in CATALOG_CATEGORIES:
        if key == cat:
            return mdb
    return "Mix"


def bucket_of(type_name: str) -> str | None:
    """分类名 → 四大类；命中脏类返回 None。"""
    name = str(type_name or "").strip()
    if not name or any(word in name for word in _JUNK_WORDS):
        return None
    if any(word in name for word in _ANIME_WORDS):
        return "anime"
    if any(word in name for word in _VARIETY_WORDS):
        return "variety"
    if any(word in name for word in _TV_WORDS):
        return "tv"
    if any(word in name for word in _MOVIE_WORDS):
        return "movie"
    return None


def _classes(payload) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    raw = payload.get("class")
    if not isinstance(raw, list):
        data = payload.get("data")
        raw = data.get("class") if isinstance(data, dict) else None
    return [row for row in (raw or []) if isinstance(row, dict)
            and row.get("type_id") is not None]


def _to_int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _params(api: str, **extra) -> dict:
    params = {"ac": "list"}
    params.update(extra)
    return params


async def _get_json(client: httpx.AsyncClient, api: str, params: dict,
                    timeout: float) -> dict | None:
    try:
        resp = await client.get(api, params=params,
                                headers={"User-Agent": cms._UA}, timeout=timeout)
        if resp.status_code != 200:
            return None
        text = resp.text
        if text.lstrip().startswith("<"):
            return cms._xml_to_json(text)
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - 单源失败不影响其他源
        return None


def _page_rows(payload: dict | None, source: dict) -> list[cms.OnlineTitle]:
    if not isinstance(payload, dict) or str(payload.get("code")) != "1":
        return []
    out = []
    for row in payload.get("list") or []:
        try:
            item = cms.map_vod(row, source)
        except Exception:  # noqa: BLE001 - 单条脏数据跳过
            continue
        if item:
            out.append(item)
    return out


class Catalog:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._routes: dict[str, dict] = {}
        self._updated_at = 0.0
        self._refreshing = False
        self._pages: dict[tuple, tuple[float, list[cms.OnlineTitle]]] = {}
        self._load()

    # ---------- 持久化 ----------
    def _load(self) -> None:
        try:
            data = json.loads(STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            routes = data.get("routes")
            if isinstance(routes, dict):
                self._routes = {k: v for k, v in routes.items()
                                if isinstance(v, dict) and v.get("type_id")}
            self._updated_at = float(data.get("updated_at") or 0)

    def _persist(self) -> None:
        try:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "updated_at": self._updated_at,
                "routes": self._routes,
            }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            tmp.replace(STATE)
        except OSError:
            pass

    # ---------- 读取 ----------
    def route(self, cat: str) -> dict | None:
        return self._routes.get(cat)

    def available(self, cat: str) -> bool:
        route = self._routes.get(cat)
        return bool(route and route.get("total"))

    def total(self, cat: str) -> int:
        return int((self._routes.get(cat) or {}).get("total") or 0)

    def stale(self) -> bool:
        return (not self._updated_at) or (time.time() - self._updated_at) >= ROUTE_TTL

    def status(self) -> dict:
        return {
            "updated_at": int(self._updated_at),
            "refreshing": self._refreshing,
            "routes": {cat: {"name": (self._routes.get(cat) or {}).get("type_name", ""),
                             "source": (self._routes.get(cat) or {}).get("source_name", ""),
                             "total": self.total(cat)}
                       for cat, _, _ in CATALOG_CATEGORIES},
        }

    def reset(self) -> None:
        self._routes = {}
        self._updated_at = 0.0
        self._pages.clear()
        try:
            STATE.unlink()
        except OSError:
            pass

    # ---------- 路由重建 ----------
    async def refresh_routes(self, client: httpx.AsyncClient,
                             sources: list[dict]) -> dict:
        if self._refreshing:
            return self.status()
        self._refreshing = True
        try:
            table = await self._discover(client, sources)
            if table:
                self._routes = table
                self._updated_at = time.time()
                self._pages.clear()
                self._persist()
        except Exception:  # noqa: BLE001 - 刷新失败保留旧路由
            pass
        finally:
            self._refreshing = False
        return self.status()

    async def _discover(self, client: httpx.AsyncClient,
                        sources: list[dict]) -> dict[str, dict]:
        classes_by_source: list[tuple[dict, list[dict]]] = []
        for source in sources[:8]:
            payload = await client_get_classes(client, source)
            classes = _classes(payload)
            if classes:
                classes_by_source.append((source, classes))
        if not classes_by_source:
            return {}

        routes: dict[str, dict] = {}
        for cat, _, _ in CATALOG_CATEGORIES:
            best = None
            for source, classes in classes_by_source[:2]:
                candidates = [row for row in classes
                              if bucket_of(str(row.get("type_name") or "")) == cat][:2]
                for row in candidates:
                    info = await self._probe(client, source, row)
                    if not info:
                        continue
                    if best is None or info["total"] > best["total"]:
                        best = info
                    if info["total"] >= 200:  # 已经够大，不必继续探测
                        break
                if best and best["total"] >= 200:
                    break
            if best:
                routes[cat] = best
        return routes

    async def _probe(self, client: httpx.AsyncClient, source: dict,
                     row: dict) -> dict | None:
        type_id = str(row.get("type_id"))
        payload = await _get_json(client, source["api"],
                                  {"ac": "detail", "t": type_id, "pg": 1},
                                  _PROBE_TIMEOUT)
        titles = _page_rows(payload, source)
        if not titles:
            return None
        # 探测页顺手入缓存，首次浏览第一页不必再打一次源站。
        self._pages[(source["api"], type_id, 1)] = (time.monotonic(), titles)
        pagecount = _to_int((payload or {}).get("pagecount"))
        total = _to_int((payload or {}).get("total"))
        if pagecount:
            total = pagecount * PER_PAGE
        elif total <= 0:
            total = len(titles) * 20
        return {
            "source_id": source.get("id") or "",
            "source_name": source.get("name") or source.get("id") or "",
            "api": source["api"],
            "type_id": type_id,
            "type_name": str(row.get("type_name") or ""),
            "pages": max(1, pagecount or (total + PER_PAGE - 1) // PER_PAGE),
            "total": max(len(titles), total),
            "posters": [t.poster for t in titles[:8] if t.poster],
        }

    # ---------- 分页读取 ----------
    async def _page(self, client: httpx.AsyncClient, route: dict,
                    page: int) -> list[cms.OnlineTitle]:
        key = (route["api"], route["type_id"], page)
        hit = self._pages.get(key)
        if hit and time.monotonic() - hit[0] < PAGE_TTL:
            return hit[1]
        payload = await _get_json(
            client, route["api"],
            {"ac": "detail", "t": route["type_id"], "pg": page}, 8.0)
        source = {"id": route.get("source_id") or "catalog",
                  "name": route.get("source_name") or "",
                  "api": route["api"]}
        titles = _page_rows(payload, source)
        if len(self._pages) > 240:
            self._pages.clear()
        self._pages[key] = (time.monotonic(), titles)
        return titles

    async def window(self, client: httpx.AsyncClient, cat: str,
                     offset: int, size: int) -> tuple[list[cms.OnlineTitle], int]:
        route = self._routes.get(cat)
        if not route:
            return [], 0
        total = int(route.get("total") or 0)
        offset = max(0, int(offset))
        size = max(1, min(int(size), 100))
        if total and offset >= total:
            return [], total
        first_page = offset // PER_PAGE + 1
        last_page = (offset + size + PER_PAGE - 1) // PER_PAGE
        titles: list[cms.OnlineTitle] = []
        for page in range(first_page, max(first_page, last_page) + 1):
            rows = await self._page(client, route, page)
            if not rows:
                # 到头：把总数收敛到真实位置，前端据此自然停止滚动。
                real_total = (page - 1) * PER_PAGE
                if not total or real_total < total:
                    route["total"] = real_total
                    self._persist()
                break
            titles.extend(rows)
        skip = offset - (first_page - 1) * PER_PAGE
        return titles[skip:skip + size], int(route.get("total") or 0)


async def client_get_classes(client: httpx.AsyncClient, source: dict):
    return await _get_json(client, source["api"], _params(source["api"]),
                           _PROBE_TIMEOUT)


catalog = Catalog()
