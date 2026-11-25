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
import dataclasses
import json
import threading
import time
from pathlib import Path

import httpx

import cms
import config
import drpy
import hongguo
import runtime

STATE = config.STATE_DIR / "catalog.json"
# 列表页独立落盘：源站（尤其 drpy 的 HTML 站）单页要 3~14 秒，只放内存的话
# 每次升级/重启后第一次进每个分类都得现场等一遍。
PAGES_STATE = config.STATE_DIR / "catalog_pages.json"
PER_PAGE = 20
PAGE_TTL = 1800.0
# 过期不立刻抛：先返回旧页、后台刷新（stale-while-revalidate），
# 避免「进分类要等十几秒才出海报墙」。超过这个时长才真正重新抓。
PAGE_STALE = 24 * 3600.0
PAGES_MAX = 240          # 内存里最多留多少页
PAGES_PERSIST = 80       # 落盘只保留最近这么多页，控制文件体积
ROUTE_TTL = 6 * 3600.0
_PROBE_TIMEOUT = 6.0

CATALOG_CATEGORIES = [
    ("movie", "🎞海量电影", "Movie"),
    ("tv", "📺海量剧集", "TV"),
    ("anime", "🌸海量动漫", "Mix"),
    ("variety", "🎤海量综艺", "Mix"),
]

# 脏类：命中即不参与分类归并（解说/预告/福利等）。
# 注意「短剧」不是脏类：它同时是合法的剧集分类（红果之类源的主分类），
# 列在 _TV_WORDS 里，放进来会让这类源的主库被默认过滤掉。
_JUNK_WORDS = ("伦理", "福利", "写真", "情色", "成人", "三级", "里番",
               "解说", "预告", "花絮", "体育", "赛事", "直播",
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


#: 列表页缓存的体积上限。老版本把分集地址一起存，实测 176MB；
#: 超过这个量级说明是旧格式，直接放弃加载而不是把它解析进内存。
PAGES_FILE_MAX = 16 * 1024 * 1024
#: 落盘用的“轻量条目”：去掉分集地址，只留海报墙/详情要用的元数据 + 集数。
_SLIM_FIELDS = ("provider", "vod_id", "title", "provider_name", "type_name",
                "year", "area", "remarks", "score", "poster", "overview",
                "actor", "director", "updated")

_PAGES_WRITE_LOCK = threading.Lock()


def _slim(t: cms.OnlineTitle) -> cms.OnlineTitle:
    """缓存副本：丢掉分集地址，用 ``ep_hint`` 保住集数。

    一页 80 条、每条上百集就是上万个 Episode 对象，几十页叠起来能把
    进程顶到 cgroup 上限；播放要用的分集由 ``_online_brief`` 按需补。
    """
    if not t.lines:
        return t
    return dataclasses.replace(t, lines={}, line_sources={},
                               ep_hint=cms.episode_count(t))


def _page_payload(key: tuple, ts: float,
                  rows: list[cms.OnlineTitle]) -> str:
    items = []
    for t in rows:
        rec = {name: getattr(t, name, "") for name in _SLIM_FIELDS}
        rec["ep_hint"] = int(t.ep_hint or cms.episode_count(t))
        items.append(rec)
    return json.dumps({"api": key[0], "type_id": key[1], "page": key[2],
                       "ts": ts, "items": items},
                      ensure_ascii=False, separators=(",", ":"))


def _write_pages(text: str) -> None:
    """线程里写盘；正在写就跳过（下一次节流到点会再写）。"""
    if not _PAGES_WRITE_LOCK.acquire(blocking=False):
        return
    try:
        PAGES_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PAGES_STATE.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(PAGES_STATE)
    except OSError as exc:  # noqa: BLE001 - 落盘失败不影响浏览
        runtime.log(f"catalog: 列表页落盘失败 {type(exc).__name__}")
    finally:
        _PAGES_WRITE_LOCK.release()


#: 抓页时顺手把条目注册进 store（分类里点开即可播，不必再等详情）。
_STORE = None


def bind_store(store) -> None:
    global _STORE
    _STORE = store


def _remember(rows: list[cms.OnlineTitle]) -> None:
    if _STORE is None or not rows:
        return
    try:
        _STORE.remember(rows)
    except Exception as exc:  # noqa: BLE001 - 入库失败不影响列表
        runtime.log(f"catalog: 条目入库失败 {type(exc).__name__}")


class Catalog:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._routes: dict[str, dict] = {}
        self._classes: dict[str, list[dict]] = {}
        self._updated_at = 0.0
        self._last_attempt = 0.0
        self._refreshing = False
        self._pages: dict[tuple, tuple[float, list[cms.OnlineTitle]]] = {}
        # 值里的 float 是**墙上时钟**（time.time），落盘后跨重启仍然有效；
        # 以前用 time.monotonic 导致缓存只能活在单次进程里。
        self._refreshing_pages: set[tuple] = set()
        self._pages_write = 0.0
        # 每页的 JSON 片段：抓取时就渲染好，落盘只要拼字符串，绝不会在事件
        # 循环里对整张缓存再做一次 dataclasses.asdict（一页上万个分集，
        # 全量序列化能把进程卡住十几秒，所有接口一起超时）。
        self._page_json: dict[tuple, str] = {}
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
        self._load_pages()

    def _load_pages(self) -> None:
        """恢复列表页缓存；只认还在 ``PAGE_STALE`` 内的记录。"""
        try:
            # 旧版本把每页的全部分集地址也存了（实测 176MB / 79 万条），
            # 启动时解析就会吃掉几百兆内存。超限直接放弃，宁可首屏重抓。
            if PAGES_STATE.stat().st_size > PAGES_FILE_MAX:
                runtime.log("catalog: 列表页缓存过大，按新版重建")
                return
            raw = json.loads(PAGES_STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        rows = raw.get("pages") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            return
        names = {f.name for f in dataclasses.fields(cms.OnlineTitle)}
        now = time.time()
        for rec in rows:
            if not isinstance(rec, dict):
                continue
            api, tid, page = rec.get("api"), rec.get("type_id"), rec.get("page")
            ts, items = rec.get("ts"), rec.get("items")
            if not (api and tid and page and isinstance(items, list)):
                continue
            if not isinstance(ts, (int, float)) or now - float(ts) > PAGE_STALE:
                continue
            restored = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    # 分集地址不落盘（一页上万个），集数靠 ep_hint 保留；
                    # 真要播放时 _online_brief 会按需补一次详情。
                    data = {k: v for k, v in item.items()
                            if k in names and k not in ("lines", "line_sources")}
                    data["lines"], data["line_sources"] = {}, {}
                    restored.append(cms.OnlineTitle(**data))
                except TypeError:
                    continue
            if restored:
                key = (str(api), str(tid), int(page))
                self._pages[key] = (float(ts), restored)
                self._page_json[key] = _page_payload(key, float(ts), restored)

    def _persist_pages(self, force: bool = False) -> None:
        """把列表页落盘：只拼已经渲染好的片段，写盘交给线程。

        以前这里对整张缓存做 ``dataclasses.asdict``——一页上万集，几十页
        加起来 79 万个对象，一次要 13 秒且**跑在事件循环上**，所有接口
        （含搜索）一起超时。现在每页抓取时就渲染好片段，落盘只是拼字符串。
        """
        now = time.time()
        if not force and now - self._pages_write < 10.0:
            return
        self._pages_write = now
        parts = []
        for key, (ts, rows) in sorted(self._pages.items(),
                                      key=lambda kv: kv[1][0],
                                      reverse=True)[:PAGES_PERSIST]:
            if not rows:
                continue
            body = self._page_json.get(key)
            if body is None:
                body = _page_payload(key, ts, rows)
                self._page_json[key] = body
            parts.append(body)
        text = '{"v":2,"pages":[' + ",".join(parts) + "]}"
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _write_pages(text)
            return
        loop.run_in_executor(None, _write_pages, text)

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

    async def warm_pages(self, client: httpx.AsyncClient, limit: int = 30,
                         gap: float = 0.4) -> int:
        """后台预热各分类首页。

        源站单页 3~14 秒，冷启动后每个分类的第一次打开都得现场等。这里在
        后台把过期/缺失的首页顺序灌进缓存，用户点进去就是现成的。串行 +
        间隔，避免把慢站打挂。

        排序上把 ``drpy`` / ``hongguo`` 排前面：CMS 站一页 1~3 秒，
        JS 源/红果常常 10 秒以上，先灌慢的收益最大；``limit`` 只数真正
        发起的抓取，命中的新鲜页不占名额，否则前面一批快源会把名额吃光，
        慢源永远排在后面。
        """
        warmed = 0
        order = sorted(
            self._routes.items(),
            key=lambda kv: 0 if str((kv[1] or {}).get("kind") or "") in
            ("drpy", "hongguo") else 1)
        for cat, route in order:
            if warmed >= limit:
                break
            if not isinstance(route, dict) or not route.get("api"):
                continue
            key = (route["api"], route.get("type_id"), 1)
            hit = self._pages.get(key)
            if hit and hit[1] and time.time() - hit[0] < PAGE_TTL:
                continue
            try:
                rows = await self._fetch_page(client, route, 1)
                if rows:
                    warmed += 1
                    # 预热只把海报墙灌进缓存，行里的分集不会留在 _pages；
                    # 顺手写进 store，用户点开热过的卡片仍然能直接播。
                    _remember(rows)
            except Exception as exc:  # noqa: BLE001 - 预热失败不影响浏览
                runtime.log(f"catalog: 预热 {cat} 失败 {type(exc).__name__}")
            await asyncio.sleep(gap)
        if warmed:
            runtime.log(f"catalog: 预热 {warmed} 个分类首页")
        return warmed

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
        self._page_json.clear()
        self._persist()
        self._persist_pages(force=True)

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
                self._page_json.clear()
                self._persist()
                self._persist_pages(force=True)
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
        per_page = PER_PAGE
        if hongguo.enabled(source):
            # 红果没有 pagecount / total，只能按首屏条数估一个量级，
            # 真实边界靠翻到空页时在 window() 里收敛。
            titles = await hongguo.page_titles(client, source, type_id, 1)
            per_page = max(1, len(titles))
            total = len(titles) * 20
        elif drpy.enabled(source):
            # drpy 源是 HTML 列表，同样没有总数；翻到空页再收敛。
            # 每页条数按首屏实测（dbku 是 48 条/页，不是 CMS 的 20），
            # 否则 offset → 页码换算会错位，翻页重复/漏条。
            titles = await drpy.page_titles(client, source, type_id, 1)
            per_page = max(1, len(titles))
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
        probe_key = (source["api"], type_id, 1)
        probe_slim = [_slim(t) for t in titles]
        probe_ts = time.time()
        self._pages[probe_key] = (probe_ts, probe_slim)
        self._page_json[probe_key] = _page_payload(probe_key, probe_ts, probe_slim)
        _remember(titles)
        self._persist_pages()
        return {
            "source_id": source.get("id") or "",
            "source_name": source.get("name") or source.get("id") or "",
            "api": source["api"],
            "kind": str(source.get("kind") or ""),
            "type_id": type_id,
            "type_name": str(row.get("name") or row.get("type_name") or ""),
            "mdb": str(row.get("mdb") or "Mix"),
            "junk": bool(row.get("junk")),
            "pages": max(1, pagecount or (total + per_page - 1) // per_page),
            "per_page": per_page,
            "total": max(len(titles), total),
            "posters": [t.poster for t in titles[:8] if t.poster],
        }

    # ---------- 分页读取 ----------
    async def _page(self, client: httpx.AsyncClient, route: dict,
                    page: int) -> list[cms.OnlineTitle]:
        """取某页条目：命中缓存直回；过期先回旧页并在后台刷新。

        源站单页常常要 3~14 秒（dbku 的综艺页实测 12 秒），如果过期就同步
        重抓，用户每进一次分类都要干等十几秒。这里改成 stale-while-revalidate：
        24 小时内的旧页立刻返回，刷新在后台做完写盘，下次进来就是新的。
        """
        key = (route["api"], route["type_id"], page)
        hit = self._pages.get(key)
        if hit:
            age = time.time() - hit[0]
            if age < PAGE_TTL:
                return hit[1]
            if age < PAGE_STALE and hit[1]:
                self._refresh_page(client, route, page, key)
                return hit[1]
        try:
            return await self._fetch_page(client, route, page)
        except Exception as exc:  # noqa: BLE001 - 源站超时/改版不该让整库 500
            runtime.log(f"catalog: {route.get('type_id')} p{page} 抓取失败 "
                        f"{type(exc).__name__}")
            return hit[1] if hit else []

    def _refresh_page(self, client: httpx.AsyncClient, route: dict,
                      page: int, key: tuple) -> None:
        """后台刷新一页（单飞，同一页不会并发重抓）。"""
        if key in self._refreshing_pages:
            return
        self._refreshing_pages.add(key)

        async def run() -> None:
            try:
                rows = await self._fetch_page(client, route, page, cache=False)
                if rows:
                    slim = [_slim(t) for t in rows]
                    ts = time.time()
                    self._pages[key] = (ts, slim)
                    self._page_json[key] = _page_payload(key, ts, slim)
                    self._persist_pages()
            except Exception as exc:  # noqa: BLE001 - 后台刷新失败保留旧页
                runtime.log(f"catalog: 后台刷新 p{page} 失败 "
                            f"{type(exc).__name__}")
            finally:
                self._refreshing_pages.discard(key)

        try:
            asyncio.get_running_loop().create_task(run())
        except RuntimeError:
            self._refreshing_pages.discard(key)

    async def _fetch_page(self, client: httpx.AsyncClient, route: dict,
                          page: int, cache: bool = True) -> list[cms.OnlineTitle]:
        key = (route["api"], route["type_id"], page)
        source = {"id": route.get("source_id") or "catalog",
                  "name": route.get("source_name") or "",
                  "api": route["api"],
                  "kind": route.get("kind") or ""}
        if hongguo.enabled(source):
            # 路由里只持久化了 id/名称，口令不在其中；翻页要取回完整源配置，
            # 否则设了 TOKEN 的部署会 401，库里看起来一片空白。
            source = hongguo.find_source(str(source["id"])) or source
            titles = await hongguo.page_titles(client, source,
                                               str(route["type_id"]), page)
        elif drpy.enabled(source):
            # 规则文本只存在完整源配置里，翻页必须取回来重放一遍。
            source = drpy.find_source(str(source["id"])) or source
            titles = await drpy.page_titles(client, source,
                                            str(route["type_id"]), page)
        else:
            payload = await _get_json(
                client, route["api"],
                {"ac": "detail", "t": route["type_id"], "pg": page}, 8.0)
            titles = _page_rows(payload, source)
        if not titles:
            # 空页多半是源站限流/临时错误页：不要用空结果覆盖已有缓存。
            return []
        if not cache:
            return titles
        if len(self._pages) > PAGES_MAX:
            # 淘汰最旧的一批，而不是整表清空（清空后所有分类都要重抓）。
            for old in sorted(self._pages.items(),
                              key=lambda kv: kv[1][0])[:PAGES_MAX // 4]:
                self._pages.pop(old[0], None)
                self._page_json.pop(old[0], None)
        # 缓存里放轻量副本（不带分集），返回值保持完整：跟着返回的分集
        # 会被写入 store，用户点开就能直接播。
        slim = [_slim(t) for t in titles]
        ts = time.time()
        self._pages[key] = (ts, slim)
        self._page_json[key] = _page_payload(key, ts, slim)
        self._persist_pages()
        return titles

    async def window(self, client: httpx.AsyncClient, cat: str,
                     offset: int, size: int) -> tuple[list[cms.OnlineTitle], int]:
        route = self.route(cat)
        if not route:
            return [], 0
        total = int(route.get("total") or 0)
        per_page = int(route.get("per_page") or 0)
        if not per_page:
            # 老路由（per_page 落盘前建的）没有每页条数。drpy/红果的页长
            # 由源站决定（dbku 是 48），用首页实测一次并写回，避免 offset
            # 按 20 换算导致翻页重复/漏条。CMS 源仍按 PER_PAGE。
            if str(route.get("kind") or "").lower() in ("drpy", "hongguo"):
                first = await self._page(client, route, 1)
                per_page = max(1, len(first)) if first else PER_PAGE
                route["per_page"] = per_page
                self._persist()
            else:
                per_page = PER_PAGE
        offset = max(0, int(offset))
        size = max(1, min(int(size), 100))
        if total and offset >= total:
            return [], total
        first_page = offset // per_page + 1
        last_page = (offset + size + per_page - 1) // per_page
        titles: list[cms.OnlineTitle] = []
        for page in range(first_page, max(first_page, last_page) + 1):
            rows = await self._page(client, route, page)
            if not rows:
                if page == 1 and total:
                    # 首页取空但总数不为 0：源站限流/超时/改版都会这样，
                    # 一次失败就把 total 归零会让整个库从列表里消失（手机端
                    # 直接看不到这张库）。保留原总数，下次请求再试。
                    runtime.log(f"catalog: {cat} 首页无数据，保留 total={total}")
                    break
                # 到头：把总数收敛到真实位置，前端据此自然停止滚动。
                real_total = (page - 1) * per_page
                if not total or real_total < total:
                    route["total"] = real_total
                    self._persist()
                break
            titles.extend(rows)
        skip = offset - (first_page - 1) * per_page
        return titles[skip:skip + size], int(route.get("total") or 0)


async def client_get_classes(client: httpx.AsyncClient, source: dict):
    # 红果服务没有苹果 CMS 的 ac=list，分类走 /api/home 的 classes。
    if hongguo.enabled(source):
        rows = await hongguo.classes(client, source)
        return {"code": 1, "class": [{"type_id": r["id"], "type_name": r["name"]}
                                     for r in rows]}
    # drpy 源按 class_parse 从首页导航抓分类。
    if drpy.enabled(source):
        rows = await drpy.classes(client, source)
        return {"code": 1, "class": [{"type_id": r["id"], "type_name": r["name"]}
                                     for r in rows]}
    return await _get_json(client, source["api"], _params(source["api"]),
                           _PROBE_TIMEOUT)


catalog = Catalog()
