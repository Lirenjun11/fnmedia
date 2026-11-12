#!/usr/bin/env python3
"""虚拟媒体库：把在线内容做成飞牛影视首页里的“媒体库”。

两层结构（与 LibreTV / 参考项目一致）：

- **展示层（榜单四库）**：豆瓣热榜缓存（charts.py）只给片名/海报/评分，
  guid 稳定；点进详情时才用片名实时聚合搜索资源站，命中后把可播放 brief
  挂到该 guid 上（``ensure_bound``），复用既有详情/选集/线路/续播/播放链。
- **内容层（海量四库）**：资源站分类深翻（catalog.py），按页直连
  ``ac=detail``，条目本身就是可播放的 brief，点开即播，无需再搜索。

所有虚拟库的 guid 用独立命名空间 md5 派生，不会和官方库/在线条目冲突；
停用开关后 mediadb/list 不再注入，官方媒体库原样返回。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time

import acl
import catalog
import charts
import cms
import config
import fake
import playstate
import runtime

CHART_LIB_GUIDS = {
    cat: hashlib.md5(f"fnmedia-vlib:chart:{cat}".encode()).hexdigest()
    for cat, _, _ in charts.CHART_CATEGORIES
}
CATALOG_LIB_GUIDS = {
    cat: hashlib.md5(f"fnmedia-vlib:catalog:{cat}".encode()).hexdigest()
    for cat, _, _ in catalog.CATALOG_CATEGORIES
}
CHART_CAT_OF = {guid: cat for cat, guid in CHART_LIB_GUIDS.items()}
CATALOG_CAT_OF = {guid: cat for cat, guid in CATALOG_LIB_GUIDS.items()}

_BIND_FILE = config.STATE_DIR / "chart_bindings.json"
_BIND_MAX = 400
_BIND_TTL = 6 * 3600.0
_FAIL_TTL = 180.0

_LOCK = threading.RLock()
_BINDS: dict[str, dict] = {}
_FAILED: dict[str, float] = {}
_INFLIGHT: dict[str, asyncio.Task] = {}


# --------------------------------------------------------------- 开关

def charts_enabled() -> bool:
    cfg = config.get()
    return bool(cfg.get("vlib_enabled", True) and cfg.get("vlib_charts", True))


def catalog_enabled() -> bool:
    cfg = config.get()
    return bool(cfg.get("vlib_enabled", True) and cfg.get("vlib_catalog", True))


def enabled() -> bool:
    return charts_enabled() or catalog_enabled()


def refresh_interval() -> float:
    return max(1.0, float(config.get().get("vlib_refresh_hours", 6))) * 3600.0


# --------------------------------------------------------------- 库卡片

def libraries() -> list[dict]:
    """构造要注入 mediadb/list 的虚拟库卡片（按固定顺序）。

    开启账号权限后只返回当前账号被授权的库；分类库还要求它的分类路由
    所属的源已被授权，否则点进去也取不到内容。
    """
    out: list[dict] = []
    if charts_enabled():
        for cat, title, mdb in charts.CHART_CATEGORIES:
            if not acl.lib_allowed(f"chart:{cat}"):
                continue
            if not charts.chart_cache.items(cat):
                continue
            posters = [fake.poster_path(url)
                       for url in charts.chart_cache.covers(cat)][:8]
            posters = [p for p in posters if p]
            out.append(_lib_card(CHART_LIB_GUIDS[cat], title, mdb, posters))
    if catalog_enabled():
        for cat, title, mdb in catalog.CATALOG_CATEGORIES:
            if not acl.lib_allowed(f"catalog:{cat}"):
                continue
            route = catalog.catalog.route(cat)
            if not route or not route.get("total"):
                continue
            if not acl.provider_allowed(route.get("source_id")):
                continue
            posters = [fake.poster_path(url)
                       for url in (route.get("posters") or [])][:8]
            posters = [p for p in posters if p]
            out.append(_lib_card(CATALOG_LIB_GUIDS[cat], title, mdb, posters))
    return out


def _lib_card(guid: str, title: str, category: str,
              posters: list[str]) -> dict:
    return {
        "guid": guid,
        "title": title,
        "poster": posters[0] if posters else "",
        "posters": posters,
        "category": category,
        "view_type": 0,
        "poster_type": 0,
        "refresh_disabled": True,
    }


def is_virtual_lib(guid: str) -> bool:
    return guid in CHART_CAT_OF or guid in CATALOG_CAT_OF


def lib_key(guid: str) -> str:
    """库 guid -> 稳定的授权键（``chart:movie`` / ``catalog:tv``）。"""
    cat = CHART_CAT_OF.get(guid)
    if cat:
        return f"chart:{cat}"
    cat = CATALOG_CAT_OF.get(guid)
    if cat:
        return f"catalog:{cat}"
    return ""


def library_keys() -> list[dict]:
    """管理页授权选项：所有可能存在的虚拟库（与是否已刷出内容无关）。"""
    out: list[dict] = []
    for cat, title, _ in charts.CHART_CATEGORIES:
        out.append({"key": f"chart:{cat}", "title": title,
                    "guid": CHART_LIB_GUIDS[cat], "kind": "chart"})
    for cat, title, _ in catalog.CATALOG_CATEGORIES:
        out.append({"key": f"catalog:{cat}", "title": title,
                    "guid": CATALOG_LIB_GUIDS[cat], "kind": "catalog"})
    return out


def lib_guids() -> set[str]:
    return set(CHART_CAT_OF) | set(CATALOG_CAT_OF)


def library_detail(guid: str) -> dict | None:
    """``GET /v/api/v1/mdb/{guid}`` 的库详情；非虚拟库或无权限返回 None。"""
    if guid in CHART_CAT_OF:
        cat = CHART_CAT_OF[guid]
        if not acl.lib_allowed(f"chart:{cat}"):
            return None
        if not charts_enabled() or not charts.chart_cache.items(cat):
            return None
        title, mdb = charts.chart_cat_title(cat), charts.chart_cat_mdb(cat)
        posters = [fake.poster_path(url)
                   for url in charts.chart_cache.covers(cat)][:8]
    elif guid in CATALOG_CAT_OF:
        cat = CATALOG_CAT_OF[guid]
        if not acl.lib_allowed(f"catalog:{cat}"):
            return None
        route = catalog.catalog.route(cat)
        if not catalog_enabled() or not route or not route.get("total"):
            return None
        if not acl.provider_allowed(route.get("source_id")):
            return None
        title, mdb = catalog.catalog_title(cat), catalog.catalog_mdb(cat)
        posters = [fake.poster_path(url)
                   for url in (route.get("posters") or [])][:8]
    else:
        return None
    posters = [p for p in posters if p]
    return {
        "guid": guid, "name": title, "category": mdb,
        "dir_list": [], "lan": "zh-CN", "auto_scrap_subtitle": 0,
        "include_adult": False, "subtitle_lan": "zh-CN",
        "poster": posters[0] if posters else "",
        "poster_type": 0, "posters": posters,
        "view_type": 0, "last_update_time": 0, "sort_num": 0,
        "auto_progress_thumb": 0, "skip_filesize": 0,
        "prefer_local_nfo": 0, "refresh_disabled": True,
    }


def library_item(guid: str) -> dict | None:
    """``GET /v/api/v1/item/{库guid}`` 的库根节点；非虚拟库返回 None。

    官方 Web/App 进入媒体库时先请求 item/<库guid>，透传给官方后端会拿到
    ``code=-6“此媒体库不存在”``，页面直接报错——必须在透传前拦截，
    返回 type=MediaDB 的库根节点，客户端才会接着调 item/list 出海报墙。
    """
    detail = library_detail(guid)
    if detail is None:
        return None
    row = _blank_card(guid, detail["name"])
    total = sum_counts().get(guid, 0)
    row.update({
        "type": "MediaDB",
        "poster": detail.get("poster") or "",
        "posters": list(detail.get("posters") or []),
        "backdrops": detail.get("poster") or "",
        "category": detail.get("category") or "",
        "can_play": 1,
        "play_item_guid": "",
        "play_error": "",
        "number_of_episodes": total,
        "local_number_of_episodes": total,
        "ancestor_guid": "",
        "ancestor_name": "",
        "ancestor_category": "",
        "logic_type": 0,
        "status": "在线",
    })
    return row


def sum_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    if charts_enabled():
        for cat, _, _ in charts.CHART_CATEGORIES:
            if not acl.lib_allowed(f"chart:{cat}"):
                continue
            n = len(charts.chart_cache.items(cat))
            if n:
                counts[CHART_LIB_GUIDS[cat]] = n
    if catalog_enabled():
        for cat, _, _ in catalog.CATALOG_CATEGORIES:
            if not acl.lib_allowed(f"catalog:{cat}"):
                continue
            route = catalog.catalog.route(cat)
            if route and not acl.provider_allowed(route.get("source_id")):
                continue
            n = catalog.catalog.total(cat)
            if n:
                counts[CATALOG_LIB_GUIDS[cat]] = n
    return counts


# --------------------------------------------------------------- 卡片

def _blank_card(guid: str, title: str) -> dict:
    return {
        "guid": guid, "video_guid": fake.video_guid(guid),
        "path": "", "runtime": 0, "lan": "zh-CN", "imdb_id": "", "trim_id": "",
        "tv_title": "", "parent_guid": "", "parent_title": "",
        "title": title, "type": "Movie", "genres": [], "poster": "",
        "poster_width": 0, "poster_height": 0, "is_favorite": 0,
        "watched": 0, "watched_ts": 0, "vote_average": "0",
        "media_stream": {"resolutions": None, "audio_type": None,
                         "color_range_type": None},
        "release_date": "", "season_number": 0, "episode_number": 0,
        "air_date": "", "number_of_seasons": 0, "number_of_episodes": 0,
        "local_number_of_seasons": 0, "local_number_of_episodes": 0,
        "status": "", "overview": "", "ancestor_guid": "",
        "ancestor_name": "", "ancestor_category": "Mix",
        "ts": 0, "duration": 0, "single_child_guid": "", "file_name": "",
        "can_play": 1, "play_item_guid": guid, "play_error": "",
    }


def chart_card(item: dict) -> dict:
    """榜单条目 → 媒体库卡片（不解析播放；详情页才搜源绑定）。"""
    cat = item["cat"]
    guid = item["guid"]
    is_movie = cat == "movie"
    row = _blank_card(guid, item.get("title") or "")
    row["type"] = "Movie" if is_movie else "TV"
    row["poster"] = fake.poster_path(item.get("cover") or "")
    row["vote_average"] = str(item.get("rating") or "0")
    row["release_date"] = item.get("year") or ""
    row["air_date"] = item.get("year") or ""
    row["number_of_seasons"] = 0 if is_movie else 1
    row["overview"] = (item.get("desc") or "")[:500]
    row["status"] = "豆瓣热榜"
    row["genres"] = list(item.get("tags") or [])[:3]
    row["ancestor_guid"] = CHART_LIB_GUIDS[cat]
    row["ancestor_name"] = charts.chart_cat_title(cat)
    row["ancestor_category"] = charts.chart_cat_mdb(cat)
    playstate.apply(row, guid)
    return row


def catalog_card(brief: dict, cat: str) -> dict:
    guid = brief["guid"]
    row = fake.search_card(brief)
    title = (brief.get("title") or "").strip()
    remarks = (brief.get("remarks") or "").strip()
    row["title"] = title + (f" ·{remarks}" if remarks else "")
    row["ancestor_guid"] = CATALOG_LIB_GUIDS[cat]
    row["ancestor_name"] = catalog.catalog_title(cat)
    row["ancestor_category"] = catalog.catalog_mdb(cat)
    playstate.apply(row, guid)
    return row


# --------------------------------------------------------------- 绑定

def _load_binds() -> None:
    try:
        raw = json.loads(_BIND_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    data = raw.get("bindings") if isinstance(raw, dict) else None
    if not isinstance(data, dict):
        return
    now = time.time()
    for guid, rec in data.items():
        if not isinstance(rec, dict) or not rec.get("brief"):
            continue
        if now - float(rec.get("ts") or 0) > _BIND_TTL:
            continue
        _BINDS[str(guid)] = {"ts": float(rec.get("ts") or now),
                             "brief": rec["brief"]}


def _save_binds() -> None:
    try:
        _BIND_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _BIND_FILE.with_suffix(".tmp")
        payload = {"updated_at": int(time.time()), "bindings": _BINDS}
        tmp.write_text(json.dumps(payload, ensure_ascii=False,
                                  separators=(",", ":")), encoding="utf-8")
        tmp.replace(_BIND_FILE)
    except OSError:
        pass


def _remember_bind(guid: str, brief: dict) -> None:
    with _LOCK:
        _BINDS[guid] = {"ts": time.time(), "brief": dict(brief)}
        if len(_BINDS) > _BIND_MAX:
            for old in list(_BINDS)[:len(_BINDS) - _BIND_MAX]:
                _BINDS.pop(old, None)
        _save_binds()


def restore(store: cms.TitleStore) -> int:
    """进程启动时把上次绑定的榜单条目恢复到内存 store。

    源可能在进程停止期间被禁用，恢复前先按当前启用源过滤一遍，否则重启后
    旧线路会“复活”，继续从禁用源播放。
    """
    dropped = 0
    with _LOCK:
        if not _BINDS:
            _load_binds()
        count = 0
        for guid, rec in list(_BINDS.items()):
            brief = rec.get("brief")
            if not isinstance(brief, dict):
                continue
            if not config.source_live(brief.get("provider")):
                _BINDS.pop(guid, None)
                dropped += 1
                continue
            store.put_brief(dict(brief))
            count += 1
        if dropped:
            _save_binds()
    if dropped:
        runtime.log(f"vlib restore: 丢弃 {dropped} 条来自已禁用源的绑定")
    return count


def _pick_match(meta: dict, titles: list[cms.OnlineTitle]) -> cms.OnlineTitle | None:
    wanted = cms.norm_title(meta.get("title"))
    if not wanted:
        return None
    year = str(meta.get("year") or "")[:4]
    best, best_score = None, 0.0
    for title in titles:
        name = cms.norm_title(title.title)
        if not name:
            continue
        if name == wanted:
            score = 100.0
        elif wanted in name or name in wanted:
            # 副标题/季集后缀可以接受，但不能短到离谱（避免“唐人街”命中“唐人街探案2”以外的杂项）。
            shorter = min(len(name), len(wanted))
            if shorter < max(2, len(wanted) * 0.6):
                continue
            score = 60.0
        else:
            continue
        if year and str(title.year or "")[:4] == year:
            score += 20
        if cms.is_tv(title) == (meta.get("cat") != "movie"):
            score += 8
        if cms.episode_count(title) > 0:
            score += 4
        if score > best_score:
            best, best_score = title, score
    return best


async def ensure_bound(store: cms.TitleStore, client, guid: str,
                       ignore_acl: bool = False) -> dict | None:
    """确保榜单 guid 已绑定「当前账号可播」的 brief；未命中返回 None。

    全局 store 里的绑定可能来自另一个账号（甚至更宽的授权集），所以这里
    先用 ``acl.filter_brief`` 判断对当前账号是否真的可用；不可用时继续走
    重绑流程，用自己的授权源重新搜一次。

    ``ignore_acl=True`` 给媒体分片链路用：播放器不带登录凭证时也能补绑，
    避免整条播放链因为「认不出是谁」而中断。
    """
    raw = store.get(guid)
    brief = acl.filter_brief(raw, enforce=not ignore_acl)
    if brief:
        return brief
    if raw and not config.source_live(raw.get("provider")):
        # 绑定来自已禁用的源：连同持久化记录一起作废，后面按当前启用源重搜。
        runtime.log(f"vlib bind stale: {guid[:8]} provider="
                    f"{raw.get('provider')} 已禁用")
    if raw and brief is None:
        runtime.log(f"vlib bind scope: {guid[:8]} 当前账号无可用线路，重新绑定")
    if not charts.chart_cache.is_chart_guid(guid):
        return None
    with _LOCK:
        cached = (_BINDS.get(guid) or {}).get("brief")
    if isinstance(cached, dict):
        if acl.filter_brief(cached, enforce=not ignore_acl):
            # store 可能因容量淘汰丢掉过它（例如翻了很多海量库页），绑定记录还在。
            return acl.filter_brief(store.put_brief(dict(cached)),
                                    enforce=not ignore_acl)
        if not config.source_live(cached.get("provider")):
            with _LOCK:
                _BINDS.pop(guid, None)
                _save_binds()
            _FAILED.pop(guid, None)
    now = time.monotonic()
    failed_at = _FAILED.get(guid)
    if failed_at and now - failed_at < _FAIL_TTL:
        return None
    task = _INFLIGHT.get(guid)
    if task and not task.done():
        try:
            return await task
        except Exception:  # noqa: BLE001 - 单飞任务失败按未命中处理
            return None
    async def run() -> dict | None:
        meta = charts.chart_cache.get(guid) or {}
        # 榜单卡片按需搜源：只能用当前账号被授权的源，否则会把未授权
        # 源的可播地址绑到卡片上，后续播放链就能绕过授权。
        sources = acl.sources(ignore_acl)
        if not sources:
            return None
        cfg = config.get()
        try:
            titles = await cms.search_all(
                client, sources, str(meta.get("title") or ""),
                timeout=float(cfg.get("search_timeout", 2.5)),
                concurrency=int(cfg.get("search_concurrency", 5)),
                enough=12)
        except Exception as exc:  # noqa: BLE001 - 搜索失败按未命中处理
            runtime.log(f"vlib bind error: {type(exc).__name__}")
            return None
        matched = _pick_match(meta, titles)
        if not matched:
            _FAILED[guid] = time.monotonic()
            runtime.log(f"vlib bind: no source for {meta.get('title', '')[:24]}")
            return None
        item = fake.brief_of(matched, guid)
        item["chart"] = {"cover": meta.get("cover") or "",
                         "rating": meta.get("rating") or 0,
                         "desc": (meta.get("desc") or "")[:1000]}
        store.put_brief(item)
        _remember_bind(guid, item)
        _FAILED.pop(guid, None)
        runtime.log(f"vlib bind: {item.get('title', '')[:24]} <- "
                    f"{matched.provider_name or matched.provider} "
                    f"lines={len(item.get('line_names') or [])}")
        return acl.filter_brief(item, enforce=not ignore_acl)

    task = asyncio.create_task(run())
    _INFLIGHT[guid] = task
    try:
        return await task
    finally:
        _INFLIGHT.pop(guid, None)


# --------------------------------------------------------------- 列表

def _page_args(body: dict, total: int) -> tuple[int, int, int]:
    try:
        page_size = int(body.get("page_size") or 22)
    except (TypeError, ValueError):
        page_size = 22
    page_size = max(1, min(page_size, 100))
    try:
        page = int(body.get("page") or body.get("p") or body.get("page_index")
                   or body.get("pn") or 1)
    except (TypeError, ValueError):
        page = 1
    page = max(1, page)
    try:
        offset = int(body.get("offset")) if body.get("offset") is not None \
            else (page - 1) * page_size
    except (TypeError, ValueError):
        offset = (page - 1) * page_size
    offset = max(0, min(offset, total))
    return page_size, offset, min(offset + page_size, total)


def _filter_rows(rows: list[dict], body: dict) -> list[dict]:
    tags = body.get("tags")
    wanted: set[str] = set()
    if isinstance(tags, dict):
        wanted = set(tags.get("type") or []) & {"Movie", "TV", "Movie,TV"}
        if "Movie,TV" in wanted:
            wanted.discard("Movie,TV")
            wanted.update({"Movie", "TV"})
    elif isinstance(tags, list):
        wanted = set(tags) & {"Movie", "TV"}
    if wanted:
        rows = [r for r in rows if r.get("type") in wanted]
    if int(body.get("is_favorite") or 0):
        rows = [r for r in rows if r.get("is_favorite")]
    keyword = str(body.get("keyword") or body.get("q") or
                  body.get("title") or "").strip().lower()
    if keyword:
        rows = [r for r in rows if keyword in str(r.get("title") or "").lower()]
    return rows


def _sort_rows(rows: list[dict], body: dict, default: str) -> list[dict]:
    column = str(body.get("sort_column") or default)
    reverse = str(body.get("sort_type") or "DESC").upper() != "ASC"
    if column in ("create_time", ""):
        return rows
    if column in ("vote_average", "rating"):
        key = lambda r: float(str(r.get("vote_average") or 0) or 0)  # noqa: E731
    elif column in ("release_date", "year"):
        key = lambda r: str(r.get("release_date") or "")  # noqa: E731
    elif column == "title":
        key = lambda r: str(r.get("title") or "")  # noqa: E731
    else:
        return rows
    return sorted(rows, key=key, reverse=reverse)


async def item_list(store: cms.TitleStore, client, body: dict,
                    guid: str) -> dict | None:
    """虚拟库的 item/list 数据；非虚拟库返回 None（交给原逻辑）。"""
    if guid in CHART_CAT_OF:
        cat = CHART_CAT_OF[guid]
        if not acl.lib_allowed(f"chart:{cat}"):
            return _listing(charts.chart_cat_title(cat),
                            charts.chart_cat_mdb(cat), 0, [])
        return await _chart_listing(body, cat)
    if guid in CATALOG_CAT_OF:
        cat = CATALOG_CAT_OF[guid]
        route = catalog.catalog.route(cat)
        if (not acl.lib_allowed(f"catalog:{cat}")
                or not route
                or not acl.provider_allowed(route.get("source_id"))):
            return _listing(catalog.catalog_title(cat),
                            catalog.catalog_mdb(cat), 0, [])
        return await _catalog_listing(store, client, body, cat)
    return None


async def _chart_listing(body: dict, cat: str) -> dict:
    title = charts.chart_cat_title(cat)
    mdb = charts.chart_cat_mdb(cat)
    if not charts_enabled():
        return _listing(title, mdb, 0, [])
    if _CLIENT[0] is not None and charts.chart_cache.stale(refresh_interval()):
        # stale-while-revalidate：后台补刷，不阻塞本次列表。
        asyncio.get_running_loop().create_task(
            charts.chart_cache.refresh(_CLIENT[0]))
    rows = [chart_card(item) for item in charts.chart_cache.items(cat)]
    rows = _sort_rows(_filter_rows(rows, body), body, "create_time")
    page_size, start, end = _page_args(body, len(rows))
    return _listing(title, mdb, len(rows), rows[start:end])


async def _catalog_listing(store: cms.TitleStore, client, body: dict,
                           cat: str) -> dict:
    title = catalog.catalog_title(cat)
    mdb = catalog.catalog_mdb(cat)
    if not catalog_enabled():
        return _listing(title, mdb, 0, [])
    page_size, start, end = _page_args(body, catalog.catalog.total(cat))
    if catalog.catalog.needs_rebuild(cat) and not catalog.catalog._refreshing:
        # 源被禁用后旧路由作废，后台按当前启用源重挑分类，不阻塞本次请求。
        asyncio.get_running_loop().create_task(
            catalog.catalog.refresh_routes(client, config.enabled_sources()))
    titles, total = await catalog.catalog.window(client, cat, start,
                                                 max(1, end - start))
    briefs = store.remember(titles) if titles else []
    return _listing(title, mdb, total, [catalog_card(brief, cat)
                                        for brief in briefs])


def _listing(name: str, category: str, total: int, rows: list[dict]) -> dict:
    return {"mdb_name": name, "mdb_category": category, "top_dir": "",
            "dir": "", "total": total, "list": rows, "jump_list": None}


# --------------------------------------------------------------- 详情

def _poster_of(brief: dict) -> str:
    cover = (brief.get("chart") or {}).get("cover") or brief.get("poster") or ""
    return fake.poster_path(cover)


async def detail(store: cms.TitleStore, client, guid: str) -> dict | None:
    """榜单条目详情：先确保绑定，再复用标准详情结构；未命中给“暂无片源”。"""
    if not charts.chart_cache.is_chart_guid(guid):
        return None
    meta = charts.chart_cache.get(guid) or {}
    # 绑定是全局共享的（A 可能先用较宽的授权绑定），展示前必须按当前账号裁剪，
    # 否则详情页会把别的账号才有的线路/源名暴露出来。
    brief = acl.filter_brief(await ensure_bound(store, client, guid))
    if not brief:
        poster = fake.poster_path(meta.get("cover") or "")
        row = _blank_card(guid, meta.get("title") or "")
        row.update({
            "type": "Movie" if meta.get("cat") == "movie" else "TV",
            "poster": poster, "backdrops": poster, "posters": poster,
            "vote_average": str(meta.get("rating") or "0"),
            "release_date": meta.get("year") or "",
            "air_date": meta.get("year") or "",
            "number_of_seasons": 0 if meta.get("cat") == "movie" else 1,
            "overview": (f"{meta.get('desc')}\n\n" if meta.get("desc") else "")
                        + "暂无可播放在线片源：可稍后重试，或直接搜索片名。",
            "can_play": 0, "play_item_guid": "", "status": "暂无片源",
            "play_error": "暂无在线片源",
        })
        playstate.apply(row, guid)
        return row

    data = fake.detail_item(brief)
    poster = _poster_of(brief)
    if poster:
        data["poster"] = poster
        data["backdrops"] = poster
        data["posters"] = poster
    chart_meta = brief.get("chart") or {}
    if chart_meta.get("desc"):
        data["overview"] = chart_meta["desc"]
    return playstate.apply(fake.adopt_images(data), guid)


# 供后台刷新使用的最新请求客户端（app 启动时注册，避免模块循环导入）。
_CLIENT: list = [None]


def bind_client(client) -> None:
    _CLIENT[0] = client


def bind_count() -> int:
    with _LOCK:
        if not _BINDS:
            _load_binds()
        return len(_BINDS)


def purge_provider(store: cms.TitleStore, provider: str) -> int:
    """删除源时清掉它的绑定与内存条目。

    源被“禁用”还能靠 provider 是否在配置里判断；源被“删除”后配置里已没有
    这个 id，只能在这里主动清，否则旧线路会一直留在内存和绑定文件里。
    """
    pid = str(provider or "")
    if not pid:
        return 0
    removed = 0
    with _LOCK:
        for guid, rec in list(_BINDS.items()):
            brief = rec.get("brief") or {}
            if str(brief.get("provider") or "") == pid:
                _BINDS.pop(guid, None)
                removed += 1
        if removed:
            _save_binds()
    removed += store.purge(
        lambda brief: str(brief.get("provider") or "") == pid)
    _FAILED.clear()
    if removed:
        runtime.log(f"vlib purge provider {pid}: {removed} 条")
    return removed


def chart_of_media(media_guid: str) -> str | None:
    """media/video guid 反查榜单 guid（未绑定前播放链拿不到 store 映射时用）。"""
    if not media_guid:
        return None
    for item in charts.chart_cache.all_items():
        guid = item.get("guid")
        if not guid:
            continue
        if media_guid in (fake.media_guid(guid), fake.video_guid(guid)):
            return guid
    return None
