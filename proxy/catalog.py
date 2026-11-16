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
import hongguo

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


def is_junk(type_name: str) -> bool:
    """解说 / 预告 / 福利等不宜展示的脏类；名字识别不出不算脏类。"""
    name = str(type_name or "").strip()
    return not name or any(word in name for word in _JUNK_WORDS)


def bucket_of(type_name: str) -> str | None:
    """分类名 → 四大类；命中脏类返回 None。"""
    name = str(type_name or "").strip()
    if is_junk(name):
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


def class_rows(payload) -> list[dict]:
    """``ac=list`` 原始分类表 → 统一结构，**保留每个 type_id 不归并**。

    脏类只是打标记（默认不勾选），不再悄悄丢掉；名字识别不出四大类的
    分类（如「4K 专区」「Netflix」）也照常列出，类型按 Mix 处理。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for row in _classes(payload):
        tid = str(row.get("type_id") or "").strip()
        name = str(row.get("type_name") or "").strip()
        if not tid or not name or tid in seen:
            continue
        seen.add(tid)
        bucket = bucket_of(name)
        out.append({
            "id": tid, "name": name,
            # 保留源站原字段名，兼容直接传原始 ac=list 行的调用方。
            "type_id": tid, "type_name": name,
            "junk": is_junk(name),
            "mdb": {"movie": "Movie", "tv": "TV"}.get(bucket, "Mix"),
        })
    return out


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
        self._classes: dict[str, list[dict]] = {}
        self._updated_at = 0.0
        self._last_attempt = 0.0
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
            raw_classes = data.get("classes")
            if isinstance(raw_classes, dict):
                self._classes = {
                    str(sid): [r for r in rows
                               if isinstance(r, dict) and r.get("id")]
                    for sid, rows in raw_classes.items()
                    if isinstance(rows, list)}
            self._updated_at = float(data.get("updated_at") or 0)

    def _persist(self) -> None:
        try:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "updated_at": self._updated_at,
                "routes": self._routes,
                "classes": self._classes,
            }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            tmp.replace(STATE)
        except OSError:
            pass

    # ---------- 源站原始分类表 ----------
    def classes(self, sid: str) -> list[dict]:
        return list(self._classes.get(str(sid or "")) or [])

    def all_classes(self) -> dict[str, list[dict]]:
        return {sid: list(rows) for sid, rows in self._classes.items()}

    async def load_classes(self, client, sources: list[dict],
                           force: bool = False) -> dict[str, list[dict]]:
        """按需拉取源站 ``ac=list`` 的全部分类并缓存，供管理页勾选。"""
        targets = [s for s in sources[:8]
                   if force or not self._classes.get(str(s.get("id") or ""))]
        if targets:
            async def fetch(source: dict):
                payload = await client_get_classes(client, source)
                return str(source.get("id") or ""), class_rows(payload)
            results = await asyncio.gather(
                *(fetch(s) for s in targets), return_exceptions=True)
            changed = False
            for result in results:
                if not isinstance(result, tuple):
                    continue
                sid, rows = result
                if sid and rows:
                    self._classes[sid] = rows
                    changed = True
            if changed:
                self._persist()
        return self.all_classes()

    # ---------- 读取 ----------
    def route(self, cat: str) -> dict | None:
        route = self._routes.get(cat)
        if not route:
            return None
        # 每条路由都绑定到某个源：源被禁用后不能再拿它的分类出列表、更
        # 不能用它的地址播放，直接视为该分类不可用（等重建选新源）。
        if not config.source_live(route.get("source_id")):
            return None
        return route

    def needs_rebuild(self, cat: str) -> bool:
        """该分类是否该重建路由（源被禁用 / 从未建立 / 过期）。"""
        if self.route(cat) is not None:
            return self.stale()
        return (time.time() - self._last_attempt) > 30.0

    def route_ids(self, sid: str) -> list[str]:
        """该源已建路由的 type_id 列表（虚拟库 guid 反查的兜底来源）。"""
        prefix = f"{str(sid or '')}:"
        return [key[len(prefix):] for key in self._routes
                if key.startswith(prefix) and key[len(prefix):]]

    def available(self, cat: str) -> bool:
        route = self.route(cat)
        return bool(route and route.get("total"))

    def total(self, cat: str) -> int:
        return int((self.route(cat) or {}).get("total") or 0)

    def stale(self, ttl: float = ROUTE_TTL) -> bool:
        """路由是否该重扫。``ttl`` 传「刷新间隔」小时数转成的秒数。

        以前写死 6 小时，管理页的「刷新间隔」只作用于榜单、对分类库无效，
        用户设 1 小时也还是等 6 小时，容易误以为有新片没更新。
        """
        return (not self._updated_at) or (time.time() - self._updated_at) >= ttl

    def status(self) -> dict:
        routes: dict[str, dict] = {}
        by_source: dict[str, dict] = {}
        for key, row in self._routes.items():
            info = {"name": row.get("type_name", ""),
                    "source": row.get("source_name", ""),
                    "mdb": row.get("mdb", "Mix"),
                    "total": self.total(key)}
            routes[key] = info
            if ":" in key:
                sid, _, tid = key.partition(":")
                by_source.setdefault(sid, {})[tid] = info
        return {
            "updated_at": int(self._updated_at),
            "refreshing": self._refreshing,
            "routes": routes,
            "by_source": by_source,
        }

    def legacy_routes(self) -> bool:
        """是否存在旧版「<sid>:四大类」路由，需要重建为 <sid>:<type_id>。"""
        cats = {row[0] for row in CATALOG_CATEGORIES}
        return any(":" in key and key.partition(":")[2] in cats
                   for key in self._routes)

    def missing_split_routes(self, sources: list[dict]) -> bool:
        """拆分模式下，映射源一个真正的 ``<sid>:<type_id>`` 路由都没有。

        典型现场：升级前路由表是四大类（``<cat>`` 或 ``<sid>:movie``），切到
        按源拆库后没有任何东西触发重建，``libraries()`` 在拆分分支里找不到
        路由就全部跳过，客户端只剩榜单四库、海量库凭空消失。``legacy_routes``
        只认 ``<sid>:movie`` 那种旧键，纯 ``<cat>`` 路由会永远卡住。
        """
        if not sources:
            return False
        cats = {row[0] for row in CATALOG_CATEGORIES}
        for source in sources:
            sid = str(source.get("id") or "")
            if not sid:
                continue
            if any(key.startswith(f"{sid}:")
                   and key.partition(":")[2] not in cats
                   for key in self._routes):
                continue
            return True
        return False

    def reset(self) -> None:
        """清内容缓存（路由 / 分页），**保留源站分类表**。

        分类表是源站元数据（type_id ↔ type_name），虚拟库 guid 要靠它反查。
        以前连它一起清掉，清缓存后到重建完成前所有「源 × 分类」库都认不出
        自己是谁，客户端表现为库点进去直接报错、重建也救不回来。重建本来
        就会重新拉一次 ``ac=list`` 覆盖它，保留不会留下旧内容。
        """
        self._routes = {}
        self._updated_at = 0.0
        self._pages.clear()
        self._persist()

    # ---------- 路由重建 ----------
    async def refresh_routes(self, client: httpx.AsyncClient,
                             sources: list[dict], split: bool = False,
                             force: bool = False) -> dict:
        if self._refreshing:
            return self.status()
        self._refreshing = True
        self._last_attempt = time.time()
        try:
            table = await self._discover(client, sources, split, force)
            if table:
                live = {str(s.get("id") or "") for s in sources}
                cats = {row[0] for row in CATALOG_CATEGORIES}
                for key, row in list(self._routes.items()):
                    if ":" in key:
                        if not split:
                            continue  # 分类模式的 <sid>:… 路由不归这次管
                        sid, _, tid = key.partition(":")
                        # 拆分模式只清「旧四大类残留」和「源已移出映射」的路由。
                        # 本次没探测到的分类**保留旧路由**：源站偶发限流/超时
                        # 时整库不会突然消失，等下次重建再覆盖。
                        if tid in cats or sid not in live:
                            self._routes.pop(key, None)
                    else:
                        if split:
                            continue  # 拆分模式不动四大类路由
                        self._routes.pop(key, None)
                self._routes.update(table)
                self._updated_at = time.time()
                self._pages.clear()
                self._persist()
            elif table is not None and split \
                    and not self.missing_split_routes(sources):
                # 本轮没有新分类可补（映射源的库都齐了）：记为「已检查」。
                # 不记的话 stale 永远为真，每 10 分钟白拉一遍所有源的分类表。
                self._updated_at = time.time()
                self._persist()
        except Exception:  # noqa: BLE001 - 刷新失败保留旧路由
            pass
        finally:
            self._refreshing = False
        return self.status()

    async def _discover(self, client: httpx.AsyncClient,
                        sources: list[dict], split: bool = False,
                        force: bool = False) -> dict[str, dict] | None:
        """探测分类表并建路由。返回 ``None`` 表示一个源的分类表都没拿到。

        区分 ``{}`` 与 ``None`` 很重要：``{}`` 是「拿到了分类表，但本轮没有
        需要新建的路由」，调用方应记为已检查；``None`` 是彻底失败，必须保留
        旧路由并等下一次重试。
        """
        async def fetch(source: dict):
            payload = await client_get_classes(client, source)
            rows = class_rows(payload)
            return (source, rows) if rows else None

        # 上限 16：映射源超过 8 个时，后面的源原本永远拿不到分类表，
        # 它们的「源×分类」库就会认不出来（清缓存后尤其明显）。
        results = await asyncio.gather(
            *(fetch(s) for s in sources[:16]), return_exceptions=True)
        classes_by_source = [row for row in results if isinstance(row, tuple)]
        if not classes_by_source:
            return None
        # 顺手缓存每个源的全部分类，管理页直接列出，不必再打一次源站。
        for source, rows in classes_by_source:
            sid = str(source.get("id") or "")
            if sid and rows:
                self._classes[sid] = rows
        if split:
            return await self._discover_by_source(client, classes_by_source,
                                                 force)

        routes: dict[str, dict] = {}
        for cat, _, _ in CATALOG_CATEGORIES:
            best = None
            for source, classes in classes_by_source[:2]:
                candidates = [row for row in classes
                              if bucket_of(row["name"]) == cat][:2]
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

    async def _discover_by_source(self, client, classes_by_source,
                                  force: bool = False) -> dict[str, dict]:
        """按「源 × 该源原始分类」逐类建路由，key 为 ``<sid>:<type_id>``。

        默认取该源全部非脏分类；配置了 ``vlib_class_ids`` 时只取选中的
        type_id（用户显式勾选脏类也能出）。每源最多 60 类、总并发 6。

        ``force=False`` 时**已有可用路由的分类不再重复探测**：重建只补缺，
        否则每次刷新都要打上百个 ``ac=detail&t=`` 请求，源站一限流就整库
        建不出来（表现为「清缓存重建后一直打不开」）。

        探测结果**边完成边写回** ``self._routes`` 并落盘（每 2 秒一次）：
        源站慢时一百多个分类要跑一两分钟，等全部探测完才写盘的话，客户端
        在这段时间里刷新只会看到榜单四库，容易被当成「库没了」。
        """
        picked_all = config.get().get("vlib_class_ids") or {}
        sem = asyncio.Semaphore(6)

        async def probe_one(source, row, sid, tid):
            async with sem:
                return sid, tid, await self._probe(client, source, row)

        tasks = []
        for source, rows in classes_by_source[:12]:
            sid = str(source.get("id") or "")
            if not sid:
                continue
            picked = picked_all.get(sid)
            if isinstance(picked, list) and picked:
                wanted = {str(x) for x in picked}
                chosen = [r for r in rows if str(r["id"]) in wanted]
            else:
                chosen = [r for r in rows if not r.get("junk")]
            for row in chosen[:60]:
                tid = str(row["id"])
                if not force and (self._routes.get(f"{sid}:{tid}")
                                  or {}).get("total"):
                    continue
                tasks.append(asyncio.create_task(
                    probe_one(source, row, sid, tid)))
        routes: dict[str, dict] = {}
        pending = set(tasks[:120])
        last_flush = 0.0
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, timeout=2.0,
                    return_when=asyncio.FIRST_COMPLETED)
                added = False
                for task in done:
                    try:
                        sid, tid, info = task.result()
                    except Exception:  # noqa: BLE001 - 单类探测失败跳过
                        continue
                    if info:
                        routes[f"{sid}:{tid}"] = info
                        added = True
                if added and time.time() - last_flush >= 2.0:
                    last_flush = time.time()
                    self._routes.update(routes)
                    self._updated_at = time.time()
                    self._persist()
        finally:
            for task in pending:
                task.cancel()
        return routes

    async def _probe(self, client: httpx.AsyncClient, source: dict,
                     row: dict) -> dict | None:
        type_id = str(row.get("id") or row.get("type_id") or "")
        pagecount = 0
        if hongguo.enabled(source):
            # 红果没有 pagecount / total，只能按首屏条数估一个量级，
            # 真实边界靠翻到空页时在 window() 里收敛。
            titles = await hongguo.page_titles(client, source, type_id, 1)
            total = len(titles) * 20
        else:
            payload = await _get_json(client, source["api"],
                                      {"ac": "detail", "t": type_id, "pg": 1},
                                      _PROBE_TIMEOUT)
            titles = _page_rows(payload, source)
            pagecount = _to_int((payload or {}).get("pagecount"))
            total = _to_int((payload or {}).get("total"))
            if pagecount:
                total = pagecount * PER_PAGE
            elif total <= 0:
                total = len(titles) * 20
        if not titles:
            return None
        # 探测页顺手入缓存，首次浏览第一页不必再打一次源站。
        self._pages[(source["api"], type_id, 1)] = (time.monotonic(), titles)
        return {
            "source_id": source.get("id") or "",
            "source_name": source.get("name") or source.get("id") or "",
            "api": source["api"],
            "kind": str(source.get("kind") or ""),
            "type_id": type_id,
            "type_name": str(row.get("name") or row.get("type_name") or ""),
            "mdb": str(row.get("mdb") or "Mix"),
            "junk": bool(row.get("junk")),
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
        source = {"id": route.get("source_id") or "catalog",
                  "name": route.get("source_name") or "",
                  "api": route["api"],
                  "kind": route.get("kind") or ""}
        if hongguo.enabled(source):
            titles = await hongguo.page_titles(client, source,
                                               str(route["type_id"]), page)
        else:
            payload = await _get_json(
                client, route["api"],
                {"ac": "detail", "t": route["type_id"], "pg": page}, 8.0)
            titles = _page_rows(payload, source)
        if len(self._pages) > 240:
            self._pages.clear()
        self._pages[key] = (time.monotonic(), titles)
        return titles

    async def window(self, client: httpx.AsyncClient, cat: str,
                     offset: int, size: int) -> tuple[list[cms.OnlineTitle], int]:
        route = self.route(cat)
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
    # 红果服务没有苹果 CMS 的 ac=list，分类走 /api/home 的 classes。
    if hongguo.enabled(source):
        rows = await hongguo.classes(client, source)
        return {"code": 1, "class": [{"type_id": r["id"], "type_name": r["name"]}
                                     for r in rows]}
    return await _get_json(client, source["api"], _params(source["api"]),
                           _PROBE_TIMEOUT)


catalog = Catalog()
