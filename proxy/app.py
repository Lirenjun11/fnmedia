#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import json
import os
import time
from urllib.parse import parse_qs, urlsplit
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import admin_api
import acl
import borrow
import briefcache
import catalog as catalog_mod
import charts
import cms
import config
import douban
import drpy
import fake
import hls
import hongguo
import playcache
import playstate
import playstate_api
import runtime
import skipmarks
import tmdb
import upstream
import vlib
import vlib_api

STATIC = Path(__file__).resolve().parent / "static"
ONLINE = "/v/media/online"
SEG_PREFIX = f"{ONLINE}/seg"


async def _auto_update_worker() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            cfg = config.get()
            if not cfg.get("auto_update") or not cfg.get("subscription_url"):
                continue
            interval = float(cfg.get("update_interval_hours") or 24) * 3600
            if time.time() - float(cfg.get("last_update") or 0) < interval:
                continue
            result = await admin_api.fetch_and_apply()
            runtime.log(f"auto subscription refresh: {result['enabled']} sources")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  
            runtime.log(f"auto subscription refresh failed: {type(exc).__name__}")


async def _vlib_worker(app: FastAPI) -> None:
    """虚拟媒体库后台刷新：榜单缓存 + 资源站分类路由。

    只在缓存过期时刷新；页面请求永远读缓存（stale-while-revalidate），
    刷新失败保留旧数据，绝不让媒体库列表变空。
    """
    await asyncio.sleep(5)
    while True:
        try:
            cfg = config.get()
            if cfg.get("vlib_enabled", True):
                interval = max(1.0, float(cfg.get("vlib_refresh_hours", 6))) * 3600
                if cfg.get("vlib_charts", True) \
                        and charts.chart_cache.stale(interval):
                    await charts.chart_cache.refresh(app.state.src)
                if cfg.get("vlib_catalog", True) \
                        and config.enabled_sources() \
                        and (catalog_mod.catalog.stale(interval)
                             or (vlib.split_by_source()
                                 and (catalog_mod.catalog.legacy_routes()
                                      or catalog_mod.catalog
                                      .missing_split_routes(
                                          vlib.mapped_sources())))):
                    # 两种「配置要求拆库但路由不是拆库形态」的现场都要重建：
                    # 1) 旧版留下 <sid>:movie 键；2) 路由表整个还是四大类
                    #    <cat>（升级/清缓存后常见），不重建客户端就只剩榜单
                    #    四库，海量库全部消失。
                    built = await catalog_mod.catalog.refresh_routes(
                        app.state.src,
                        vlib.mapped_sources() or config.enabled_sources(),
                        split=vlib.split_by_source())
                    runtime.log("catalog rebuilt: "
                                f"routes={len(built.get('routes') or {})} "
                                f"split={vlib.split_by_source()}")
                if cfg.get("vlib_catalog", True) and config.enabled_sources():
                    # 预热各分类首页：源站单页要 3~14 秒，先把首屏灌进缓存，
                    # 用户点进分类就不用干等（后台串行，不影响请求）。
                    await catalog_mod.catalog.warm_pages(app.state.src)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  
            runtime.log(f"vlib refresh failed: {type(exc).__name__}")
        await asyncio.sleep(600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.up = upstream.make_client(
        os.environ.get("FNOS_MEDIA_UPSTREAM_SOCK", "/var/run/trim_media_upstream.sock"),
        os.environ.get("FNOS_MEDIA_UPSTREAM_URL", ""),
    )
    app.state.src = httpx.AsyncClient(
        timeout=httpx.Timeout(8.0, connect=3.0),
        limits=httpx.Limits(max_connections=40, max_keepalive_connections=12),
        follow_redirects=True, trust_env=False,
    )
    app.state.poster = httpx.AsyncClient(
        timeout=httpx.Timeout(5.0, connect=3.0),
        limits=httpx.Limits(max_connections=24, max_keepalive_connections=8),
        follow_redirects=True, trust_env=False,
    )
    app.state.store = cms.TitleStore()
    # 分类预热抓到的页只把轻量元数据留在页缓存里（分集太占内存），
    # 完整条目注册进 store，用户点开热过的卡片仍然能直接播。
    catalog_mod.bind_store(app.state.store)
    playstate.load()
    vlib.bind_client(app.state.src)
    restored = vlib.restore(app.state.store)
    # 内存 store 重启即空，最近播放/收藏要靠落盘缓存恢复，否则列表会全空。
    played = playcache.restore(app.state.store)
    # 虚拟库/搜索结果同理：客户端缓存着库里的卡片，重启后没有 brief
    # 就点不开详情（透传官方直接 -6「此媒体库不存在」）。
    browsed = briefcache.restore(app.state.store)
    runtime.log(f"service started, sources={len(config.enabled_sources())} "
                f"aes_decrypt={'on' if hls.can_decrypt() else 'off'} "
                f"vlib_charts={charts.chart_cache.total()} restored={restored} "
                f"playcache={played} browse={browsed}")
    worker = asyncio.create_task(_auto_update_worker())
    vlib_worker = asyncio.create_task(_vlib_worker(app))
    try:
        yield
    finally:
        vlib_worker.cancel()
        worker.cancel()
        await asyncio.gather(vlib_worker, worker, return_exceptions=True)
        playstate.persist(force=True)
        await app.state.up.aclose()
        await app.state.src.aclose()
        await app.state.poster.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url="/v/api/ext/openapi.json")


@app.middleware("http")
async def access_log(request: Request, call_next):
    started = time.monotonic()
    path = request.url.path
    # 身份只在官方/媒体接口上解析；管理页自身（/v/api/ext/*）也需要，用于
    # 自检与「绑定当前设备」，所以一并覆盖。静态资源不做无谓的库查询。
    if path.startswith("/v/api/") or path.startswith(ONLINE) \
            or path.startswith("/online/"):
        try:
            ident_token = acl.push(acl.identify(request))
        except Exception as exc:  # noqa: BLE001 - 识别故障按未识别处理，不能 500
            runtime.log(f"acl identify failed: {type(exc).__name__}")
            ident_token = acl.push(acl.Identity(via="unknown",
                                                reason="identify-error"))
    else:
        ident_token = acl.push(acl.Identity(via="off"))
    try:
        resp = await call_next(request)
    except Exception as exc:  
        runtime.log(f"ERROR {request.method} {path} {type(exc).__name__}")
        raise
    finally:
        acl.reset(ident_token)
    if path.endswith("/task/running") or path.startswith("/v/api/ext/logs"):
        return resp
    if "/sys/img/" in path:
        if resp.status_code >= 400 or "online/" in path:
            runtime.log(f"{request.method} {path} -> {resp.status_code}")
        return resp
    if "/api/" in path or path.startswith(ONLINE) or path.startswith("/online/") or path.startswith("/_ext/"):
        runtime.log(f"{request.method} {path} -> {resp.status_code} "
                    f"{int((time.monotonic() - started) * 1000)}ms")
        if path.startswith(ONLINE) and resp.status_code >= 400:
            runtime.log(f"media error {path} -> {resp.status_code}")
    return resp


def _ua(request: Request) -> str:
    return request.headers.get("user-agent", "")


def _is_ios(request: Request) -> bool:
    """识别 iOS 端（含 Flutter / Alamofire 两种网络栈）。

    新 iOS（1.13.0 起带 fncache）对 media/range 的 302 只取清单、不再继续请求
    分片，必须与官方一样在同一请求内直接返回 200 播放内容。
    """
    ua = _ua(request).lower()
    if "tvos" in ua:
        return False
    return ("com.trim.media.ios" in ua or "iphone" in ua
            or "ipad" in ua or "alamofire" in ua)


async def _passthrough(request: Request):
    try:
        return await upstream.forward(request, request.app.state.up)
    except Exception as exc:  
        runtime.log(f"upstream error: {type(exc).__name__}")
        return JSONResponse({"code": -1, "msg": "官方影视服务暂不可用", "data": None},
                            status_code=502)


async def _passthrough_stream(request: Request):
    try:
        return await upstream.stream(request, request.app.state.up)
    except Exception as exc:  
        runtime.log(f"upstream stream error: {type(exc).__name__}")
        return JSONResponse({"code": -1, "msg": "官方影视服务暂不可用", "data": None},
                            status_code=502)


_BAD_LINES: dict = {}
_BAD_TTL = 600.0
_FAILOVER_ATTEMPTS = 3


def _bad_lines(guid: str) -> set:
    now = time.monotonic()
    for key, ts in list(_BAD_LINES.items()):
        if now - ts > _BAD_TTL:
            _BAD_LINES.pop(key, None)
    return {line for (g, line) in _BAD_LINES if g == guid}


def _mark_bad_line(guid: str, line: int) -> None:
    _BAD_LINES[(guid, line)] = time.monotonic()


def _ordered_lines(brief: dict, start: int, bad: set) -> list[int]:
    total = len(brief.get("line_names") or [])
    if not total:
        return []
    order = []
    for offset in range(total):
        candidate = (start + offset) % total
        if candidate in bad:
            continue
        order.append(candidate)
        if len(order) >= _FAILOVER_ATTEMPTS:
            break
    if not order:
        order.append(start % total)
    return order


async def _race_lines(client, brief: dict, guid: str, index: int,
                      start: int, budget: float):
    candidates = _ordered_lines(brief, start, _bad_lines(guid))
    task_line = {}
    tasks = []
    for line in candidates:
        picked = _pick(brief, line, index)
        if not picked:
            continue
        url, _ = picked
        try:
            url = hls.ensure_public_target(url)
        except ValueError:
            _mark_bad_line(guid, line)
            continue
        task = asyncio.create_task(
            hls.fetch_playlist(client, url, timeout=budget))
        task_line[task] = line
        tasks.append(task)
    if not tasks:
        return None
    deadline = time.monotonic() + budget
    pending = set(tasks)
    winner = None
    while pending and winner is None:
        remain = deadline - time.monotonic()
        if remain <= 0:
            break
        done, pending = await asyncio.wait(pending, timeout=remain,
                                           return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            line = task_line[task]
            if task.cancelled() or task.exception():
                _mark_bad_line(guid, line)
                continue
            text, base, meta = task.result()
            winner = (line, text, base, meta)
            break
    for task in tasks:
        if task.done() and not task.cancelled():
            task.exception()
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return winner


def _pick(brief: dict, line: int, index: int) -> tuple[str, str] | None:
    names = brief.get("line_names") or []
    if not names:
        return None
    li = max(0, min(line, len(names) - 1))
    eps = brief["lines"].get(names[li]) or []
    if not eps:
        return None
    idx = max(0, min(index, len(eps) - 1))
    return eps[idx].get("url", ""), names[li]


_DIRECT_EXT = (".mp4", ".mkv", ".flv", ".mov", ".m4v", ".webm", ".avi")


def _is_direct_media(url: str) -> bool:
    """只有明确是视频文件才是直链；分享页/未知地址按 HLS 处理。"""
    path = str(url or "").split("?", 1)[0].lower()
    # 红果服务的分集流是 /api/stream/<id>，没有扩展名但确实是 MP4（支持 Range）。
    if hongguo.STREAM_MARK in path:
        return True
    return ".m3u8" not in path and path.endswith(_DIRECT_EXT)


def _err(msg: str, status: int = 404) -> JSONResponse:
    return JSONResponse({"code": -1, "msg": msg, "data": None}, status_code=status)


@app.get("/_ext/healthz")
async def healthz():
    return {"ok": True, "service": "fnmedia-ext",
            "sources": len(config.enabled_sources())}


@app.get("/v/online-admin")
async def admin_page():
    index = STATIC / "index.html"
    if not index.exists():
        return HTMLResponse("<!doctype html><meta charset='utf-8'>"
                            "<h1>fnmedia</h1><p>前端产物未构建。</p>")
    token = config.token()
    page = index.read_text(encoding="utf-8").replace(
        "</head>",
        f'<meta name="fnos-token" content="{html_lib.escape(token, quote=True)}"></head>',
        1,
    )
    resp = HTMLResponse(page, headers={"Cache-Control": "no-store"})
    resp.set_cookie(admin_api.COOKIE, token, httponly=True,
                    samesite="lax", path="/")
    return resp


if STATIC.is_dir():
    app.mount("/v/ext", StaticFiles(directory=str(STATIC)), name="ext")

app.include_router(admin_api.router)
app.include_router(playstate_api.router)
app.include_router(vlib_api.router)


_SEARCH_LATEST: dict[str, float] = {}


def _search_session(request: Request) -> str:
    """Group rapid searches by client session; never log the raw credential."""
    raw = (request.headers.get("authorization")
           or request.headers.get("cookie") or "")
    if not raw:
        host = request.client.host if request.client else ""
        raw = host + "|" + _ua(request)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


async def _search_is_latest(request: Request, debounce_ms: int) -> bool:
    """Server-side debounce: only the last query in a burst reaches the CMS."""
    session = _search_session(request)
    token = time.monotonic()
    _SEARCH_LATEST[session] = token
    await asyncio.sleep(debounce_ms / 1000.0)
    if _SEARCH_LATEST.get(session) != token:
        return False
    _SEARCH_LATEST.pop(session, None)
    return True


@app.get("/v/api/v1/search/list")
async def search_list(request: Request):
    started = time.monotonic()
    local = await _passthrough(request)
    sources = acl.sources()
    keyword = request.query_params.get("q", "").strip()
    if not sources or not keyword:
        return local
    cfg = config.get()
    # 服务端防抖封顶 500ms：配置里设 1 秒时，每次搜索都白等一秒，
    # 叠上聚合预算就顶到手机端的超时线了；500ms 仍能吸收连续输入。
    debounce_ms = min(int(cfg.get("search_debounce_ms", 200)), 500)
    if debounce_ms > 0 and not await _search_is_latest(request, debounce_ms):
        # A newer query arrived while waiting: return official local results
        # only. The last request of the burst is the one that queries sources.
        return local
    try:
        data = json.loads(local.body)
    except (ValueError, AttributeError):
        return local
    if not isinstance(data, dict):
        return local
    items = data.get("data") if isinstance(data.get("data"), list) else []
    try:
        titles = await cms.search_all(
            request.app.state.src, sources, keyword,
            timeout=float(cfg.get("search_timeout", 2.5)),
            concurrency=int(cfg.get("search_concurrency", 8)),
        )
    except Exception as exc:  
        runtime.log(f"search error: {type(exc).__name__}")
        titles = []
    if cfg.get("merge_sources", True):
        titles = cms.merge_titles(titles)
    briefs = request.app.state.store.remember(titles)
    _prewarm_search(request, briefs)
    cards = fake.normalize_cards(
        [playstate.apply(fake.search_card(b), b["guid"]) for b in briefs],
        _ua(request))
    data["data"] = cards + items if cfg.get("online_first", True) else items + cards
    # 一行摘要：日志里能直接看出「搜索失败」是超时、源被授权裁掉、还是
    # 聚合到了但卡片被过滤，不必再猜。
    runtime.log(f"search '{keyword[:20]}' srcs={len(sources)} "
                f"titles={len(titles)} cards={len(cards)} "
                f"{time.monotonic() - started:.1f}s")
    return JSONResponse(data, status_code=200,
                        headers={"Content-Type": "application/json; charset=utf-8"})


@app.get("/v/api/v1/item/detail")
async def item_detail_query(request: Request):
    guid = str(request.query_params.get("guid") or
               request.query_params.get("item_guid") or "")
    if not guid:
        return await _passthrough(request)
    return await item_detail(guid, request)


@app.get("/v/api/v1/item/{guid}")
async def item_detail(guid: str, request: Request):
    store = request.app.state.store
    lib_item = vlib.library_item(guid)
    if lib_item:
        # 进入媒体库先要库根节点；透传会拿到官方 -6「此媒体库不存在」。
        return JSONResponse(fake.ok(
            fake.normalize_detail(lib_item, _ua(request))))
    if charts.chart_cache.is_chart_guid(guid):
        row = await vlib.detail(store, request.app.state.src, guid)
        if row:
            return JSONResponse(fake.ok(fake.normalize_detail(row, _ua(request))))
    if not _brief(store, guid) and not store.resolve_media(guid) \
            and not store.resolve_episode(guid):
        owner = vlib.chart_of_media(guid)
        if owner:
            # 未绑定榜单条目：先把线路搜出来，后面的 media/episode 分支才认得。
            await vlib.ensure_bound(store, request.app.state.src, owner)
    ep = store.resolve_episode(guid)
    if ep:
        brief = _brief(store, ep[0])
        if not brief and await _ensure_vlib(store, request.app.state.src, ep[0]):
            # 该集挂在已禁用的源上：重绑后再取一次，详情页才不会空白。
            brief = _brief(store, ep[0])
        if not brief:
            return await _passthrough(request)
        row = playstate.apply(fake.episode_item(brief, ep[1]), guid)
        return JSONResponse(fake.ok(fake.normalize_detail(row, _ua(request))))
    tv_guid = store.resolve_season(guid)
    if tv_guid:
        brief = _brief(store, tv_guid)
        if not brief and await _ensure_vlib(store, request.app.state.src, tv_guid):
            brief = _brief(store, tv_guid)
        if not brief:
            return await _passthrough(request)
        row = playstate.apply(fake.season_item(brief), guid)
        return JSONResponse(fake.ok(fake.normalize_detail(row, _ua(request))))
    if not _brief(store, guid):
        resolved = store.resolve_media(guid)
        if resolved:
            tv_guid, index = resolved
            brief = _brief(store, tv_guid)
            if brief:
                if guid in (fake.media_guid(tv_guid), fake.video_guid(tv_guid)):
                    return await _item_title_detail(brief, tv_guid, request)
                row = playstate.apply(fake.episode_item(brief, index), guid)
                return JSONResponse(fake.ok(
                    fake.normalize_detail(row, _ua(request))))
    brief = _brief(store, guid)
    if not brief:
        return await _passthrough(request)
    return await _item_title_detail(brief, guid, request)


async def _item_title_detail(brief: dict, guid: str, request: Request):
    # 红果类条目：详情接口才有分集，标题详情页是补线路的最佳时机。
    brief = await _online_brief(request, brief) or brief
    # TMDB 命中缓存立即用；未命中最多等 1 秒，超时先返回源站数据，后台继续补。
    meta = None
    if tmdb.enabled():
        try:
            meta = await tmdb.enrich(brief)
        except Exception as exc:
            runtime.log(f"tmdb enrich error: {type(exc).__name__}")
    detail = fake.detail_item(brief, meta)
    if not brief.get("lines"):
        # 补不出线路（源站不可达 / 该部署只有 JS 源）：明确标成不可播，
        # 免得客户端显示一个点了没反应的播放按钮。
        detail.update({"can_play": 0, "status": "暂无片源",
                       "play_error": "暂无在线片源"})
    borrowed = False
    if config.get().get("borrow_official", True):
        try:
            detail = borrow.enrich_detail(detail, brief.get("title"),
                                          brief.get("year"))
            borrowed = bool(detail.pop("_borrowed", False))
        except Exception as exc:
            runtime.log(f"borrow error: {type(exc).__name__}")
    if config.get().get("douban_enabled") and not borrowed:
        # 豆瓣只有竖版大图；它会把 backdrops 一起覆盖成竖图。已有 TMDB
        # 横版背景时保留横图，否则详情页顶部会变成被裁切的竖版海报。
        tmdb_backdrop = detail.get("backdrops") if meta and meta.get("backdrop") else ""
        try:
            detail = await douban.enrich_poster(
                request.app.state.poster, detail,
                brief.get("title"), brief.get("year"))
            if tmdb_backdrop:
                detail["backdrops"] = tmdb_backdrop
        except Exception as exc:
            runtime.log(f"douban enrich error: {type(exc).__name__}")
    # 豆瓣补图与官方库借用的海报都是外链，必须转成本站代理，否则客户端
    # 直连图床被防盗链拦掉，详情页顶部背景只剩黑色。
    detail = fake.adopt_images(detail)
    detail = playstate.apply(detail, guid)
    runtime.log(f"detail poster: {str(detail.get('posters'))[:100]!r} "
                f"backdrops={str(detail.get('backdrops'))[:100]!r}")
    return JSONResponse(fake.ok(fake.normalize_detail(detail, _ua(request))))


@app.get("/v/api/v1/season/list/{guid}")
async def season_route(guid: str, request: Request):
    store = request.app.state.store
    tv_guid = _tv_guid_of(store, guid)
    brief = _brief(store, tv_guid) if tv_guid else None
    if not brief and await _ensure_vlib(store, request.app.state.src, guid):
        tv_guid = _tv_guid_of(store, guid)
        brief = _brief(store, tv_guid) if tv_guid else None
    if not brief:
        return await _passthrough(request)
    rows = fake.season_list(brief)
    ua = _ua(request)
    if not fake.is_browser_client(ua):
        # 手机播放器用季卡上的集数显示“共 N”；飞牛 Web 会把带集数的合成季
        # 当成真实季而隐藏选集入口，所以浏览器端保持 0。
        total = len(fake.episode_list(brief))
        for row in rows:
            row["number_of_episodes"] = total
            row["local_number_of_episodes"] = total
    return JSONResponse(fake.ok(fake.normalize_cards(rows, ua)))


@app.post("/v/api/v1/season/list/{guid}")
async def season_route_post(guid: str, request: Request):
    return await season_route(guid, request)


def _apply_episode_progress(rows: list, tv_guid: str) -> list:
    """选集列表里给“最后观看的那一集”回填进度，客户端才显示续播条。"""
    for row in rows:
        if not isinstance(row, dict):
            continue
        pos = playstate.episode_ts(tv_guid, str(row.get("guid") or ""))
        if pos:
            row["ts"] = pos
            row["watched_ts"] = pos
    return rows


@app.get("/v/api/v1/episode/list/{guid}")
async def episode_route(guid: str, request: Request):
    store = request.app.state.store
    tv_guid = _tv_guid_of(store, guid)
    brief = _brief(store, tv_guid) if tv_guid else None
    if not brief and await _ensure_vlib(store, request.app.state.src, guid):
        tv_guid = _tv_guid_of(store, guid)
        brief = _brief(store, tv_guid) if tv_guid else None
    if not brief:
        return await _passthrough(request)
    rows = _apply_episode_progress(fake.episode_list(brief), tv_guid)
    return JSONResponse(fake.ok(fake.normalize_cards(rows, _ua(request))))


@app.post("/v/api/v1/episode/list/{guid}")
async def episode_route_post(guid: str, request: Request):
    return await episode_route(guid, request)


def _listing(rows: list) -> dict:
    return {"mdb_name": "", "mdb_category": "", "top_dir": "", "dir": "",
            "total": len(rows), "list": rows, "jump_list": None}


@app.post("/v/api/v1/item/list")
async def item_list_route(request: Request):
    """部分客户端用 item/list + parent_guid 拉取季/集子项。"""
    body = await _body(request)
    store = request.app.state.store
    lib = str(body.get("ancestor_guid") or body.get("parent_guid") or "")
    if vlib.is_virtual_lib(lib):
        data = await vlib.item_list(store, request.app.state.src, body, lib)
        if data is not None:
            data["list"] = fake.normalize_cards(data["list"], _ua(request))
            return JSONResponse(fake.ok(data))
    parent = str(body.get("parent_guid") or body.get("ancestor_guid") or "")
    if not parent:
        return await _passthrough(request)
    brief = _brief(store, parent)
    if brief and brief.get("is_tv"):
        rows = fake.normalize_cards(fake.season_list(brief), _ua(request))
        return JSONResponse(fake.ok(_listing(rows)))
    tv_guid = store.resolve_season(parent)
    brief = _brief(store, tv_guid) if tv_guid else None
    if brief:
        rows = _apply_episode_progress(fake.episode_list(brief), tv_guid)
        rows = fake.normalize_cards(rows, _ua(request))
        return JSONResponse(fake.ok(_listing(rows)))
    return await _passthrough(request)


@app.get("/v/api/v1/stream/list/{guid}")
async def stream_list_route(guid: str, request: Request):
    return await _stream_list_impl(guid, request)


@app.post("/v/api/v1/stream/list")
async def stream_list_post(request: Request):
    body = await _body(request)
    guid = str(body.get("guid") or body.get("item_guid") or "")
    if not guid:
        return await _passthrough(request)
    return await _stream_list_impl(guid, request)


async def _stream_list_impl(guid: str, request: Request):
    store = request.app.state.store
    if not store.resolve_episode(guid) and not _brief(store, guid):
        await _ensure_vlib(store, request.app.state.src, guid)
    ep = store.resolve_episode(guid)
    if ep:
        tv_guid, ep_index = ep
        eg = guid
        brief = _brief(store, tv_guid)
    else:
        brief = _brief(store, guid)
        if not brief:
            return await _passthrough(request)
        tv_guid = guid
        # 搜索结果 / 分类卡只有标题，没有分集；不在这里补一次，线上源条目
        # 的 stream/list 会返回 0 条分集，客户端判定「没有可播流」，表现为
        # 搜索里点播放毫无反应（play/info 反而会补，所以重试又好了）。
        brief = await _online_brief(request, brief) or brief
        # 续播集和 eg 必须补完线路后再算：补之前 is_tv 还是 False。
        ep_index = _resume_index(store, tv_guid) if brief.get("is_tv") else 0
        eg = fake.episode_guid(tv_guid, ep_index) if brief.get("is_tv") else ""
    if not brief:
        return await _passthrough(request)
    if ep:
        mg, vg = fake.ep_media_guid(guid), fake.ep_video_guid(guid)
    elif eg:
        mg, vg = fake.ep_media_guid(eg), fake.ep_video_guid(eg)
    else:
        mg, vg = fake.media_guid(guid), fake.video_guid(guid)
    meta = hls.get_playlist_meta((tv_guid, 0, ep_index))
    if not (meta or {}).get("height"):
        # 红果的真实档位藏在播放响应头里。这次起播前还没解析到 HLS 元数据时，
        # 用上次播放记住的档位回填，避免固定显示 1080P。
        hg_meta = (hongguo.cached_quality_for_episode(brief, ep_index)
                   or hongguo.cached_quality_for_source(
                       hongguo.find_source(str(brief.get("provider") or ""))))
        if hg_meta:
            meta = dict(meta or {}, **hg_meta)
    runtime.log(f"stream/list: {brief.get('title', '')[:30]} ep{ep_index + 1} "
                f"lines={len(brief.get('line_names') or [])} "
                f"ua={_ua(request)[:40]!r}")
    return JSONResponse(fake.ok(
        fake.stream_list_payload(brief, mg, vg, meta)))


async def _body(request: Request) -> dict:
    try:
        return json.loads(await request.body() or b"{}")
    except ValueError:
        return {}


def _dump_play(tag: str, body: dict, ref: dict | None = None) -> None:
    """临时诊断：记录客户端传来的标识字段（定位「选第 N 集却播第 1 集」）。

    只写 stdout（uvicorn 会转进 journal），不改任何行为。定位完即删。
    """
    try:
        fields = {"mg": body.get("media_guid"), "vg": body.get("video_guid"),
                  "ig": body.get("item_guid"), "g": body.get("guid"),
                  "idx": body.get("index"), "ep": body.get("episode"),
                  "eidx": body.get("episode_index"),
                  "sidx": body.get("season_index"),
                  "vi": body.get("video_index"),
                  "start": body.get("startTimestamp") or body.get("start_ts"),
                  "ts": body.get("ts")}
        picked = {k: v for k, v in fields.items() if v not in (None, "")}
        tail = " -> None"
        if ref:
            tail = (f" -> idx={ref['index']} "
                    f"ep={(ref.get('episode') or '')[:10]}")
        print(f"[playdump] {tag} keys={sorted(body)} {picked}{tail}",
              flush=True)
    except Exception:  # noqa: BLE001 - 诊断失败绝不能影响播放
        pass


def _resume_index(store: cms.TitleStore, guid: str) -> int:
    episode = playstate.episode_for(guid)
    if episode:
        resolved = store.resolve_episode(episode)
        if resolved and resolved[0] == guid:
            return resolved[1]
    media = playstate.media_for(guid)
    if media:
        resolved = store.resolve_media(media)
        if resolved and resolved[0] == guid:
            return resolved[1]
    return 0


def _tv_guid_of(store: cms.TitleStore, guid: str) -> str | None:
    ep = store.resolve_episode(guid)
    if ep:
        return ep[0]
    resolved = store.resolve_media(guid)
    if resolved:
        return resolved[0]
    season = store.resolve_season(guid)
    if season:
        return season
    return guid if _brief(store, guid) else None


def _skip_keys(store: cms.TitleStore, guid: str) -> tuple[str, list[str]]:
    """把客户端给的身份归一化成「基础影片」标记键，并返回读侧候选键。

    客户端可能传集 guid、季 guid、剧级 guid 或 media/video guid：能反查的都
    归到剧/片级，这样换集、换线路共用同一份片头/片尾秒数。反查是内存索引，
    重启或缓存淘汰后会失败，此时原样当键、并把原始 guid 放进候选键，标记不会
    因为一次反查失败而读不到。
    """
    raw = str(guid or "")
    if not raw:
        return "", []
    key = _tv_guid_of(store, raw) or raw
    aliases = [fake.season_guid(key), raw]
    return key, [a for i, a in enumerate(aliases) if a and a != key
                 and a not in aliases[:i]]


def _brief(store: cms.TitleStore, guid: str, enforce: bool = True) -> dict | None:
    """取可播放 brief，并按「源是否启用 / 当前账号是否被授权」裁剪线路。

    只在内存里按 guid 命中是不够的：用户把某个源禁用后，之前搜到的线路、
    持久化的榜单绑定、分类库缓存都还挂在这个 guid 上，播放链会继续从
    已禁用的源取流。而在开启账号权限后，跨源合并卡里的线路必须按当前
    账号的授权集裁剪，否则 A 的卡片会把 B 未授权的源一起带出去。
    这些判断统一收敛到 ``acl.filter_brief``，所有详情/播放链路自动生效。

    ``enforce=False`` 只给媒体分片链路用（播放器可能不带登录凭证）。
    """
    return acl.filter_brief(store.get(guid), enforce=enforce)


def _prewarm_search(request: Request, briefs: list[dict],
                    limit: int = 3) -> None:
    """搜索结果一出来就在后台补前几条的线路。

    搜索结果本身只有标题，点播放时才去源站查详情；源站单页 3~14 秒，现场
    等就是「点了没反应」或「播放不了」。这里先把前几条补好写回 store，
    用户点开时直接命中。
    """
    targets = [b for b in briefs
               if isinstance(b, dict) and b.get("provider")
               and not b.get("lines")][:limit]
    if not targets:
        return

    async def run() -> None:
        for brief in targets:
            try:
                await _online_brief(request, brief)
            except Exception as exc:  # noqa: BLE001 - 预热失败不影响搜索
                runtime.log(f"search prewarm failed: {type(exc).__name__}")
            await asyncio.sleep(0.2)

    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        pass


async def _online_brief(request: Request, brief: dict | None) -> dict | None:
    """在线源条目补播放地址（红果 / drpy JS 源）。

    搜索结果与分类列表本身不带分集地址（要再查一次详情），所以在详情页、
    起播、分片这三条入口统一补一次；补完写回 store，后续请求直接命中。
    """
    if not brief or brief.get("lines"):
        return brief
    source = hongguo.find_source(str(brief.get("provider") or ""))
    if hongguo.enabled(source):
        try:
            updated = await hongguo.ensure_lines(
                request.app.state.src, request.app.state.store, brief)
        except Exception as exc:  # noqa: BLE001 - 补失败保留原条目
            runtime.log(f"hongguo ensure error: {type(exc).__name__}")
            return brief
        return updated or brief
    source = drpy.find_source(str(brief.get("provider") or ""))
    if not drpy.enabled(source):
        # CMS 源（bd/bd12 等）：内存吃紧时线路会被卸掉，重启后也要能播。
        return await cms.ensure_lines(
            request.app.state.src, request.app.state.store, brief) or brief
    try:
        updated = await drpy.ensure_lines(
            request.app.state.src, request.app.state.store, brief)
    except Exception as exc:  # noqa: BLE001 - 补失败保留原条目
        runtime.log(f"drpy ensure error: {type(exc).__name__}")
        return brief
    return updated or brief


async def _drpy_play_url(request: Request, brief: dict, line: int,
                         index: int, url: str) -> str:
    """drpy 源的分集地址是「播放页」，起播前换成真实 m3u8 / mp4。

    换到的直链写回 brief：后面的 preset / main / direct 三条链路与预热都直接
    命中，不会为同一集重复解析。解析不出来就原样返回，让上层报错而不是
    把一个 HTML 页面当清单去解析。
    """
    text = str(url or "")
    if not text.startswith(("http://", "https://")):
        return text
    if _is_direct_media(text) or ".m3u8" in text.lower():
        return text
    source = drpy.find_source(str(brief.get("provider") or ""))
    if not drpy.enabled(source):
        # CMS 采集站常把上游的播放页 HTML 直接塞进 vod_play_url（例如 bd
        # 源给的是 dbku.tv/vodplay/*.html）。本条不是 drpy 源时，按域名
        # 借同站的 drpy 规则解析，否则把 HTML 当清单抓只会超时 / 502。
        source = drpy.find_source_for_url(text)
        if not source:
            return text
    got = await drpy.resolve(request.app.state.src, source, text)
    if not got:
        return text
    names = brief.get("line_names") or []
    name = names[line] if 0 <= line < len(names) else ""
    eps = (brief.get("lines") or {}).get(name) or []
    if 0 <= index < len(eps) and isinstance(eps[index], dict):
        eps[index]["url"] = got
        eps[index]["direct"] = True
        try:
            request.app.state.store.put_brief(brief)
        except Exception as exc:  # noqa: BLE001 - 写回失败不影响本次播放
            runtime.log(f"drpy put_brief failed: {type(exc).__name__}")
    runtime.log(f"drpy play: ep{index + 1} -> {got[:70]}")
    return got


def _identity(store: cms.TitleStore, guid: str) -> tuple[str, str, str] | None:
    ep = store.resolve_episode(guid)
    if ep:
        # 集映射还在、但所属源已被禁用时不算命中，交给上层重绑。
        if _brief(store, ep[0]):
            return guid, fake.ep_media_guid(guid), fake.ep_video_guid(guid)
        return None
    if _brief(store, guid):
        return guid, fake.media_guid(guid), fake.video_guid(guid)
    return None


async def _ensure_vlib(store: cms.TitleStore, client, guid: str,
                       ignore_acl: bool = False) -> bool:
    """榜单卡片在真正播放/取详情前才搜源绑定；这里做一次懒绑定。

    传入的可能是剧 guid、集 guid、media/video guid：先看内存映射能不能反查
    到榜单条目，再退回按 media guid 遍历榜单，避免“源被禁用后集级播放入口
    找不到可重绑的条目”。
    """
    owner = guid if charts.chart_cache.is_chart_guid(guid) else None
    if not owner:
        ep = store.resolve_episode(guid)
        if ep and charts.chart_cache.is_chart_guid(ep[0]):
            owner = ep[0]
    if not owner:
        media = store.resolve_media(guid)
        if media and charts.chart_cache.is_chart_guid(media[0]):
            owner = media[0]
    if not owner:
        owner = vlib.chart_of_media(guid)
    if not owner:
        return False
    await vlib.ensure_bound(store, client, owner, ignore_acl=ignore_acl)
    return True


def _play_ref(store: cms.TitleStore, value: str) -> dict | None:
    """把客户端给的任意标识（item/media/video/剧 guid）解析成统一播放引用。

    搜索结果的播放链只传 media_guid，虚拟库进来的客户端可能传 item_guid、
    甚至直接传剧/集 guid，所以四条链路（play/info、play/play、stream、
    media/range）都走这一个入口，避免某条入口认不出标识而透传给官方。
    """
    value = str(value or "")
    if not value:
        return None
    ep = store.resolve_episode(value)
    if ep and _brief(store, ep[0]):
        return {"guid": ep[0], "index": ep[1], "media": value,
                "video": fake.ep_video_guid(value), "episode": value}
    media = store.resolve_media(value)
    if media:
        guid, index = media
        if _brief(store, guid):
            return {"guid": guid, "index": index, "media": value,
                    "video": _video_guid_for(guid, index, value), "episode": ""}
    if _brief(store, value):
        return {"guid": value, "index": 0, "media": fake.media_guid(value),
                "video": fake.video_guid(value), "episode": ""}
    return None


async def _resolve_play_ref(store: cms.TitleStore, client,
                            body: dict) -> dict | None:
    """按客户端可能使用的字段顺序解析播放引用；未绑定的榜单条目现场绑定。"""
    for key in ("media_guid", "video_guid", "item_guid", "guid"):
        value = str(body.get(key) or "")
        if not value:
            continue
        ref = _play_ref(store, value)
        if ref:
            return ref
        if await _ensure_vlib(store, client, value):
            ref = _play_ref(store, value)
            if ref:
                return ref
    return None


async def _media_target(request: Request, mg: str, enforce: bool = True):
    """按 media/video guid 找可播条目；store 淘汰或榜单未绑定时现场补绑。"""
    store = request.app.state.store
    resolved = store.resolve_media(mg)
    if not resolved and await _ensure_vlib(
            store, request.app.state.src, mg, ignore_acl=not enforce):
        resolved = store.resolve_media(mg)
    if not resolved:
        return None
    guid, index = resolved
    brief = _brief(store, guid, enforce=enforce)
    if not brief and await _ensure_vlib(
            store, request.app.state.src, guid, ignore_acl=not enforce):
        # 命中的是已禁用源的旧映射：重绑后再取一次。
        brief = _brief(store, guid, enforce=enforce)
    if not brief:
        return None
    return guid, index, brief


def _clamp_line(value: str, names: list[str],
                brief: dict | None = None) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        number = int(raw)
        return number if 0 <= number < len(names) else None
    for i, name in enumerate(names):
        if name == raw or fake.line_label(name) == raw \
                or (brief is not None and fake.line_display(brief, name) == raw):
            return i
    return None


def _line_from_body(brief: dict, body: dict) -> int:
    names = brief.get("line_names") or []
    found = fake.line_index(body.get("bitrate"), names)
    if found is not None:
        return found
    index = body.get("direct_link_quality_index")
    if index is not None:
        try:
            number = int(index)
        except (TypeError, ValueError):
            number = -1
        if 0 <= number < len(names):
            return number
    wanted = str(body.get("line") or "")
    if wanted:
        found = _clamp_line(wanted, names, brief)
        if found is not None:
            return found
    return 0


def _video_guid_for(guid: str, index: int, mg: str) -> str:
    if mg in (fake.media_guid(guid), fake.video_guid(guid)):
        return fake.video_guid(guid)
    return fake.ep_video_guid(fake.episode_guid(guid, index))


def _range_selection(request: Request, brief: dict) -> tuple[int, bool]:
    """原生播放器切清晰度会带 Play-Link 或 line/bitrate 回到 Range 接口。"""
    names = brief.get("line_names") or []
    link = request.query_params.get("playlink") or \
        request.headers.get("Play-Link", "")
    params = dict(request.query_params)
    if link:
        try:
            parsed = parse_qs(urlsplit(link).query)
        except ValueError:
            parsed = {}
        for key in ("line", "bitrate", "direct_link_quality_index"):
            if key in parsed and key not in params:
                params[key] = parsed[key][0]
    # 客户端可能把新的 bitrate 追加到旧 play_link 上，优先于残留的 line。
    found = fake.line_index(params.get("bitrate"), names)
    if found is not None:
        return found, True
    index = params.get("direct_link_quality_index")
    if index not in (None, ""):
        try:
            number = int(index)
        except (TypeError, ValueError):
            number = -1
        if 0 <= number < len(names):
            return number, True
    if params.get("line"):
        found = _clamp_line(params["line"], names, brief)
        if found is not None:
            return found, True
    return 0, False


def _play_start(request: Request) -> int:
    """从 media/range 请求里取续播点（秒）。

    iOS 继续播放时客户端把 play_link（带 &start=N）放进 Play-Link 头或
    playlink 参数回传，续播点就在它的 query 里；也可能直接作为 media/range
    自身的 query。两种都要认，否则 302 到 preset 时 start 被丢，播放器从 0 起播。
    范围与 play/play 保持一致：5s 以下不当续播，超过 12 小时视为异常。
    """
    values = [request.query_params.get(k)
              for k in ("start", "startTimestamp", "start_ts")]
    link = request.query_params.get("playlink") or \
        request.headers.get("Play-Link", "")
    if link:
        try:
            parsed = parse_qs(urlsplit(link).query)
        except ValueError:
            parsed = {}
        for key in ("start", "startTimestamp", "start_ts"):
            if key in parsed:
                values.append(parsed[key][0])
    for value in values:
        try:
            number = int(float(value or 0))
        except (TypeError, ValueError):
            continue
        if 5 <= number <= 43200:
            return number
    return 0


@app.post("/v/api/v1/play/info")
async def play_info_route(request: Request):
    body = await _body(request)
    store = request.app.state.store
    ref = await _resolve_play_ref(store, request.app.state.src, body)
    _dump_play("info", body, ref)
    if not ref:
        runtime.log(f"play/info unresolved keys={sorted(body)}")
        return await _passthrough(request)
    guid, mg, vg = ref["guid"], ref["media"], ref["video"]
    if ref["episode"]:
        brief = _brief(store, guid)
        if not brief:
            return await _passthrough(request)
        brief = await _online_brief(request, brief) or brief
        index = ref["index"]
        info = fake.episode_play_info(brief, index, ref["episode"])
        canonical = guid
    else:
        brief = _brief(store, guid)
        if not brief:
            return await _passthrough(request)
        brief = await _online_brief(request, brief) or brief
        if brief.get("is_tv"):
            # 集级 media/video guid 已经带了对的集号，绝不能被续播覆盖：
            # 详情页选集后起播走的正是这条路径，覆盖回去就会变成第 1 集
            # 或上次看到的那一集。只有「剧级 guid」才是从上次继续。
            if ref["media"] != fake.media_guid(guid):
                index = ref["index"]
            else:
                index = _resume_index(store, guid)
            eg = fake.episode_guid(guid, index)
            info = fake.episode_play_info(brief, index, eg)
        else:
            index = 0
            # 起播不等待富化：只读已缓存结果（纯内存，零网络）。
            info = fake.play_info(brief, mg, vg, guid, tmdb.cached(brief))
        canonical = guid
    # 续播点必须按「集」取：进度记录每部剧只存最后观看那一集的 ts，直接用
    # 剧级 key 取会把上一集的秒数塞给新一集，客户端拿 watched_ts 直接 seek，
    # 表现为「当前集没播完就切下一集，从上一集的进度接着播」。
    if brief.get("is_tv"):
        pos = playstate.episode_ts(canonical, fake.episode_guid(canonical, index))
    else:
        pos = playstate.ts_for(canonical)
    info["ts"] = pos
    # 官方 Web/App 的续播点是 play/info 响应里的 item.watched_ts（客户端
    # 直接拿它 seek），只写外层 ts 不会被读取。
    item = info.get("item")
    if isinstance(item, dict):
        playstate.apply(item, canonical)
        item["ts"] = pos
        item["watched_ts"] = pos
        meta = hls.get_playlist_meta((canonical, 0, index)) or {}
        if not item.get("duration") and meta.get("duration"):
            item["duration"] = int(meta["duration"])
    runtime.log(f"play/info: {brief.get('title', '')[:30]} "
                f"ep{index + 1} ts={pos} resume={pos > 0}")
    # 播放页一打开就开始抓清单，等播放器真正请求 media/range/preset 时已就绪。
    _prewarm_online(request, brief, canonical, index, 0)
    return JSONResponse(fake.ok(fake.normalize_detail(info, _ua(request))))


@app.post("/v/api/v1/play/setConfigByItem")
async def play_set_config_by_item(request: Request):
    """原生「跳过片头/片尾」写入口（body 就是配置对象，无 data 包装）。

    官方播放器在菜单里设完秒数后调这个接口；标记按基础影片/季保存，所以同一
    部剧换线路、换集都不用重设。

    响应体踩过两次坑，别随手改回去：

    * 第一版只给 ``{guid, play_config:{...}}``、第二版把 ``skip_opening`` /
      ``skip_ending`` 平铺到 ``data`` 上，客户端都照样弹「影视服务出错，请检查
      服务」——但写入其实成功了、跳片也生效。说明官方成功的响应体不是我们猜的
      这几种形状。
    * 所以现在不再猜：把原请求透传给官方影视服务，官方返回 ``code=0`` 就直接
      把它那份原文回给客户端（形状必然匹配）；官方不认（未知 guid / 鉴权失败）
      才退回我们自己拼的成功体。
    * ``guid`` 回显请求里的原 guid，不是归一化后的剧级 key。
    """
    body = await _body(request)
    store = request.app.state.store
    raw = str(body.get("guid") or body.get("item_guid")
              or body.get("parent_guid") or "")
    key, aliases = _skip_keys(store, raw)
    if not key:
        return JSONResponse({"code": 400, "msg": "缺少 guid", "data": None},
                            status_code=400)
    try:
        skipmarks.set_mark(key, body)
    except ValueError as exc:
        return JSONResponse({"code": 400, "msg": str(exc), "data": None},
                            status_code=400)
    config_ = skipmarks.play_config(key, *aliases)
    msg = (f"play/setConfigByItem: {key[:8]} "
           f"片头={config_['skip_opening']}s 片尾={config_['skip_ending']}s")
    runtime.log(msg)
    # runtime.log 只在内存里，出问题要进 journal 才能回溯客户端传了什么。
    print(f"[skipmarks] {msg} raw={raw[:64]} body={str(body)[:300]}", flush=True)

    # 官方的响应形状才是客户端真正期待的那一份。把原请求原样转给官方：
    # 官方认（code=0）就直接拿它的原文回客户端，一字不改；官方不认（未知
    # guid、鉴权失败等）才退回我们自己拼的成功体，功能照旧不受影响。
    # 两个分支都把官方原文打进 journal，下次再对不上就有对照物。
    try:
        probe = await upstream.forward(request, request.app.state.up)
        raw_up = bytes(getattr(probe, "body", b"") or b"")
        print(f"[skipmarks] upstream status={probe.status_code} "
              f"len={len(raw_up)} body={raw_up[:300]!r}", flush=True)
        if probe.status_code == 200 and b'"code":0' in raw_up.replace(b" ", b""):
            return probe
    except Exception as exc:
        print(f"[skipmarks] upstream err {type(exc).__name__}: {exc}", flush=True)

    echo = raw or key
    data = {
        "guid": echo,
        "item_guid": echo,
        "skip_opening": config_["skip_opening"],
        "skip_ending": config_["skip_ending"],
        "play_config": {**config_, "guid": echo},
    }
    runtime.log(f"skipmarks fallback shape used for {key[:8]}")
    print("[skipmarks] fallback shape: data=null "
          f"(would be {json.dumps(data, ensure_ascii=False)[:200]})", flush=True)
    # 兜底用官方「写操作成功」的最小形状：同一个播放器模块的 play/record、
    # play/quit 都是 {code:0,msg:"",data:null}，客户端从不报错。带内容的
    # data 反而被拒过两次，说明这个模块对 data 的期望不是「配置对象」。
    return JSONResponse({"code": 0, "msg": "", "data": None})


@app.get("/v/api/ext/skip-marks")
async def ext_skip_marks_get(request: Request):
    """管理台用：读取某部影片/剧当前生效的跳过标记。"""
    store = request.app.state.store
    key, aliases = _skip_keys(store, request.query_params.get("guid") or "")
    if not key:
        return JSONResponse({"code": 400, "msg": "缺少 guid", "data": None},
                            status_code=400)
    return JSONResponse(fake.ok({"guid": key, "mark": skipmarks.get(key, *aliases),
                                 "play_config": skipmarks.play_config(key, *aliases)}))


@app.delete("/v/api/ext/skip-marks")
async def ext_skip_marks_delete(request: Request):
    """管理台用：清掉某部影片/剧的跳过标记（客户端菜单重置即走这里）。"""
    store = request.app.state.store
    key, aliases = _skip_keys(store, request.query_params.get("guid") or "")
    if not key:
        return JSONResponse({"code": 400, "msg": "缺少 guid", "data": None},
                            status_code=400)
    skipmarks.delete(key, *aliases)
    return JSONResponse(fake.ok(True))


@app.post("/v/api/v1/stream")
async def stream_post(request: Request):
    body = await _body(request)
    store = request.app.state.store
    ref = await _resolve_play_ref(store, request.app.state.src, body)
    _dump_play("stream", body, ref)
    if not ref:
        runtime.log(f"stream unresolved keys={sorted(body)}")
        return await _passthrough(request)
    guid, index, mg = ref["guid"], ref["index"], ref["media"]
    brief = _brief(store, guid)
    if not brief:
        return await _passthrough(request)
    brief = await _online_brief(request, brief) or brief
    line = _line_from_body(brief, body)
    _prewarm_online(request, brief, guid, index, line)
    meta = hls.get_playlist_meta((guid, line, index))
    data = fake.stream_payload(
        brief, mg,
        str(body.get("video_guid") or ref["video"]),
        meta, line)
    runtime.log(f"stream: {brief.get('title', '')[:30]} ep{index + 1} "
                f"line={line}/{len(brief.get('line_names') or [])} "
                f"bitrate={body.get('bitrate')!r} ua={_ua(request)[:40]!r}")
    return JSONResponse(fake.ok(data))


@app.post("/v/api/v1/person/list/{guid}")
async def person_list_route(guid: str, request: Request):
    store = request.app.state.store
    target = _tv_guid_of(store, guid)
    brief = _brief(store, target) if target else None
    if not brief:
        return await _passthrough(request)
    people = None
    if config.get().get("borrow_official", True):
        try:
            people = borrow.person_rows(brief.get("title"), brief.get("year"))
        except Exception as exc:
            runtime.log(f"borrow person error: {type(exc).__name__}")
    if not people:
        people = fake.person_list(brief)["list"]
    return JSONResponse(fake.ok({"total": len(people), "list": people[:40]}))


@app.get("/v/api/v1/media/range/{file_hash}")
async def media_range_route(file_hash: str, request: Request):
    """手机 App 原画直链兜底：订阅源条目没有官方文件，App 拿 fake GUID 请求
    media/range 时官方必然 403。命中订阅源映射时改由我们供数：mp4 直链做
    Range 代理（支持拖动），HLS 源重定向到本模块 m3u8 播放链路；官方真实
    文件的 hash 永远透传，不受影响。"""
    store = request.app.state.store
    ref = _play_ref(store, file_hash)
    if not ref and await _ensure_vlib(store, request.app.state.src, file_hash):
        ref = _play_ref(store, file_hash)
    _dump_play("range", {"guid": file_hash,
                         "line": request.query_params.get("line"),
                         "index": request.query_params.get("index")}, ref)
    if not ref:
        return await _passthrough(request)
    guid, index, mg = ref["guid"], ref["index"], ref["media"]
    brief = _brief(store, guid)
    if not brief:
        return await _passthrough(request)
    brief = await _online_brief(request, brief) or brief
    line, explicit = _range_selection(request, brief)
    picked = _pick(brief, line, index)
    if not picked:
        return Response(status_code=404)
    url = picked[0]
    if not str(url).startswith(("http://", "https://")):
        # 红果 JS 源占位地址：明确失败，不要当 HLS 去解析。
        return Response(status_code=404)
    url = await _drpy_play_url(request, brief, line, index, url)
    is_hls = not _is_direct_media(url)
    runtime.log(f"media/range takeover: {brief.get('title', '')[:30]} "
                f"ep{index + 1} line{line} -> {'hls' if is_hls else 'direct'}")
    if is_hls:
        auto = "&auto=1" if not explicit else ""
        if _is_ios(request):
            # iOS（电影/剧集统一）：先预热，再 302 到 preset(mr)。
            # App 会跟随到 .m3u8 地址；preset(mr) 直接返回媒体清单，
            # 不再下发 master（1.13 拿到 master 后不跟变体）。
            # 继续播放的 start 在 Play-Link / query 里，必须一起带给 preset，
            # 否则它会从 0 起播。
            _warm_playlist_task(request, brief, guid, index, line)
            location = (f"{ONLINE}/{mg}/preset.m3u8?line={line}"
                        f"&index={index}{auto}&from=mr")
            start = _play_start(request)
            if not start:
                # 1.13 续播不回传起点（Play-Link 是 App 内部 UUID），从播放
                # 记录取：服务端把清单裁到断点，不依赖客户端注入/seek。
                # 剧集只认“最后观看的那一集”，避免用别集进度错位裁剪。
                try:
                    if brief.get("is_tv"):
                        start = playstate.episode_ts(
                            guid, fake.episode_guid(guid, index))
                    else:
                        start = playstate.ts_for(guid)
                except Exception:
                    start = 0
            if 5 <= start <= 43200:
                # 超过片长 95% 视为“已看完重看”，从头播。
                meta = hls.get_playlist_meta((guid, line, index)) or {}
                total = float(meta.get("duration") or 0)
                if not (total > 0 and start > total * 0.95):
                    location += f"&start={start}"
            return Response(status_code=302, headers={"Location": location})
        # 其它端保持原逻辑：302 到媒体清单，避免多包一层 master 等待。
        return Response(status_code=302, headers={
            "Location": f"{ONLINE}/{mg}/main.m3u8?line={line}"
                        f"&index={index}{auto}"})
    try:
        url = hls.ensure_public_target(url)
    except ValueError:
        _mark_bad_line(guid, line)
        return Response(status_code=404)
    retry = ()
    if hongguo.STREAM_MARK in url:
        alt = hongguo.compat_url(url)
        retry = (alt,) if alt else ()
    started = time.monotonic()
    resp = await hls.proxy_media(request, url, request.app.state.src,
                                 timeout=hls._MEDIA_READ_TIMEOUT,
                                 on_headers=hongguo.quality_hook(url),
                                 retry_urls=retry)
    runtime.log(f"media/range direct: {brief.get('title', '')[:24]} "
                f"ep{index + 1} {resp.status_code} "
                f"{time.monotonic() - started:.1f}s")
    return resp


@app.post("/v/api/v1/play/play")
async def play_play(request: Request):
    body = await _body(request)
    store = request.app.state.store
    ref = await _resolve_play_ref(store, request.app.state.src, body)
    _dump_play("play", body, ref)
    if not ref:
        runtime.log(f"play/play unresolved keys={sorted(body)}")
        return await _passthrough(request)
    guid, index, mg = ref["guid"], ref["index"], ref["media"]
    brief = _brief(store, guid)
    if not brief:
        return await _passthrough(request)
    brief = await _online_brief(request, brief) or brief
    explicit = any(body.get(k) for k in
                   ("bitrate", "line", "direct_link_quality_index"))
    line = _line_from_body(brief, body)
    bad = _bad_lines(guid)
    if line in bad:
        order = _ordered_lines(brief, line, bad)
        if order:
            line = order[0]
    picked = _pick(brief, line, index)
    if not picked:
        return _err("该片暂无可用播放线路", 404)
    url, _ = picked
    if not str(url).startswith(("http://", "https://")):
        # 红果 JS 源的占位地址（hgplay://）：本扩展只接管内置源，
        # 直接给明确提示，别让客户端拿到一个必然失败的地址。
        return _err("该线路暂不支持，请在红果服务里切换内置源", 404)
    url = await _drpy_play_url(request, brief, line, index, url)
    is_hls = not _is_direct_media(url)
    suffix = "preset.m3u8" if is_hls else "direct"
    if not is_hls:
        # 红果直链：起播前先让源站把地址取好，切集不再干等 10–30 秒；
        # 剧集顺带预热下一集，连续追剧时切集几乎无等待。
        nxt = ""
        if brief.get("is_tv"):
            ahead = _pick(brief, line, index + 1)
            nxt = str(ahead[0]) if ahead else ""
        _prewarm_hongguo(request.app.state.src, url, nxt)
    # 保持相对地址：App 会按自己配置的服务器地址（含 5666 端口）解析。
    # 不能按 Host 拼绝对地址——反代会把 Host 写成不带端口的 $host，
    # 拼出来会丢端口，App 请求到 80 端口报「视频文件不存在」。
    link = f"{ONLINE}/{mg}/{suffix}?line={line}&index={index}"
    if not explicit:
        link += "&auto=1"
    try:
        start = int(float(body.get("startTimestamp")
                          or body.get("start_ts") or 0))
    except (TypeError, ValueError):
        start = 0
    if 5 <= start <= 43200:
        # 超过片长 95% 的起点视为“已看完重看”，从头开始，避免一进去就结束。
        meta = hls.get_playlist_meta((guid, line, index)) or {}
        total = float(meta.get("duration") or 0)
        if total > 0 and start > total * 0.95:
            start = 0
        else:
            link += f"&start={start}"
    runtime.log(f"play: {brief.get('title', '')[:30]} ep{index + 1} "
                f"line={line}/{len(brief.get('line_names') or [])} "
                f"bitrate={body.get('bitrate')!r} auto={not explicit} "
                f"start={start} mg={mg[:8]} {suffix} "
                f"ua={_ua(request)[:40]!r}")
    return JSONResponse(fake.play_response(
        mg, str(body.get("video_guid") or ref["video"]),
        link, "HLS" if is_hls else "MP4"))


@app.get(f"{ONLINE}/{{mg}}/preset.m3u8")
async def preset_m3u8(mg: str, request: Request):
    """播放 master：只列当前线路。手机端会逐条预取 master 的所有变体，
    多变体会把它拖到超时；换线由 qualities -> play/play 重新下发单变体。"""
    target = await _media_target(request, mg, enforce=False)
    _dump_play("preset", {"mg": mg,
                          "line": request.query_params.get("line"),
                          "index": request.query_params.get("index")},
               {"index": target[1]} if target else None)
    if not target:
        runtime.log(f"preset miss: {mg[:12]}")
        return Response(status_code=404)
    guid, index, brief = target
    # 红果条目只有 vod_id 时先把分集线路补齐，否则直链判定会失败、
    # 误走 master 分支，iOS/App 拿到 master 后不跟变体就一直转圈。
    brief = await _online_brief(request, brief) or brief
    line, _ = _range_selection(request, brief)
    names = brief.get("line_names") or []
    line = max(0, min(line, len(names) - 1)) if names else 0
    selected_pick = _pick(brief, line, index)
    if selected_pick and _is_direct_media(selected_pick[0]):
        return Response(status_code=302, headers={
            "Location": f"{ONLINE}/{mg}/direct?line={line}&index={index}"})

    if request.query_params.get("from") == "mr" or _is_ios(request):
        # 剧集经 media/range -> preset 进来：App 只认 .m3u8 地址上的媒体清单，
        # 这里直接返回 main（不再下发 master，避免它不跟变体）。
        # iOS 1.13（fncache）从 play/play 的 play_link 直接请求 preset 时没有
        # from=mr，也会落到这个 master 分支；它拿到 master 后不请求变体，
        # 表现就是「继续播放」一直转圈。凡是 iOS 一律直接下发媒体清单，
        # start 由下面转为 main 的 query，最终注入 EXT-X-START。
        scope = dict(request.scope)
        scope["path"] = f"{ONLINE}/{mg}/main.m3u8"
        q = f"line={line}&index={index}&ph=1"
        start = request.query_params.get("start")
        if start:
            q += f"&start={start}"
        scope["query_string"] = q.encode()
        resp = await main_m3u8(mg, Request(scope))
        body = resp.body if isinstance(resp.body, (bytes, bytearray)) else b""
        segs = sum(1 for ln in body.splitlines()
                   if ln.strip() and not ln.startswith(b"#"))
        ph = " ph=1" if resp.headers.get("X-fnmedia-Placeholder") == "1" else ""
        runtime.log(f"preset(mr): {brief.get('title', '')[:30]} ep{index + 1} "
                    f"line{line} -> {resp.status_code}{ph} segs={segs}")
        return resp

    # 与 media/range/main 共用同一个单飞预热任务：避免 preset 与 main 各自
    # 再抓一次清单，冷启动最多只等一次。
    selected = _warm_playlist_task(request, brief, guid, index, line)
    if selected is not None:
        try:
            await asyncio.wait_for(asyncio.shield(selected),
                                   timeout=min(float(config.get().get(
                                       "play_timeout", 8.0)), 2.0))
        except asyncio.TimeoutError:
            pass
        except Exception as exc:  # noqa: BLE001
            # shield 会把预热任务的异常原样抛给等待方：源站这次返回了
            # HTML/错误页（``not a playlist``）时，这里不接住就是 500，
            # 客户端表现为「转圈几秒后播放失败，点重试才成功」。
            runtime.log(f"preset warm failed: {type(exc).__name__}")
    label = (fake.line_display(brief, names[line]) if names
             else "原画").replace('"', "")
    # 根路径绝对地址：iOS 经 media/range 302 到 preset 后，会以原始
    # media/range 地址为基准解析相对 URI，写成 main.m3u8 会变成
    # /v/api/v1/media/range/main.m3u8（错误路径）。
    main_uri = f"{ONLINE}/{mg}/main.m3u8?line={line}&index={index}"
    try:
        start = int(float(request.query_params.get("start") or 0))
    except ValueError:
        start = 0
    if start > 0:
        main_uri += f"&start={start}"
    text = ("#EXTM3U\n#EXT-X-VERSION:3\n"
            f'#EXT-X-STREAM-INF:BANDWIDTH=2000000,PROGRAM-ID=1,NAME="{label}"\n'
            f"{main_uri}\n")
    return Response(text, media_type="application/vnd.apple.mpegurl",
                    headers={"Access-Control-Allow-Origin": "*",
                             "Cache-Control": "no-store",
                             "Connection": "close"})


_PLACEHOLDER_DIR = Path(__file__).resolve().parent / "assets" / "placeholder"
_PH_SEG_DUR = 2.0
_PH_WINDOW = 3
_IOS_READY_BUDGET = 2.2
_IOS_WARM_TIMEOUT = 8.0
_IOS_WARM: dict[tuple, asyncio.Task] = {}
# 预热只是提速手段，不能和搜索/海报抢资源：同时最多 2 条清单。
_WARM_SEM = asyncio.Semaphore(2)


def _placeholder_playlist(mg: str) -> Response:
    """线路清单还没就绪时先回直播式占位清单（200），让播放器保持轮询而不是报错。

    占位分片用真实 TS（assets/placeholder/placeholder.ts）；播放器每 2s 重拉
    media/range，后台清单就绪后下一次轮询即切回正片。
    """
    seq = int(time.time() // int(_PH_SEG_DUR))
    lines = ["#EXTM3U", "#EXT-X-VERSION:3",
             f"#EXT-X-TARGETDURATION:{int(_PH_SEG_DUR)}",
             f"#EXT-X-MEDIA-SEQUENCE:{seq}",
             "#EXT-X-DISCONTINUITY-SEQUENCE:0"]
    for i in range(_PH_WINDOW):
        lines.append(f"#EXTINF:{_PH_SEG_DUR:.3f},")
        lines.append(f"{ONLINE}/{mg}/_placeholder/placeholder.ts?n={seq + i}")
    return Response("\n".join(lines) + "\n",
                    media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store",
                             "Access-Control-Allow-Origin": "*",
                             "Connection": "close",
                             "X-fnmedia-Placeholder": "1"})


@app.get(f"{ONLINE}/{{mg}}/_placeholder/placeholder.ts")
async def placeholder_ts(mg: str, request: Request):
    path = _PLACEHOLDER_DIR / "placeholder.ts"
    if not path.exists():
        return Response(status_code=404)
    return Response(path.read_bytes(), media_type="video/mp2t",
                    headers={"Cache-Control": "public, max-age=86400",
                             "Access-Control-Allow-Origin": "*"})


def _warm_playlist_task(request: Request, brief: dict, guid: str,
                        index: int, line: int) -> asyncio.Task | None:
    """单飞预热清单：首个 media/range 只等 2.2s，剩余时间在后台继续抓。"""
    key = (guid, line, index)
    if hls.get_playlist_text(key):
        return None  # 已缓存：不要再抓一次
    task = _IOS_WARM.get(key)
    if task is not None and not task.done():
        return task
    picked = _pick(brief, line, index)
    if not picked:
        return None
    try:
        url = hls.ensure_public_target(picked[0])
    except ValueError:
        return None
    client = request.app.state.src

    async def run():
        async with _WARM_SEM:
            text, base, meta = await asyncio.wait_for(
                hls.fetch_playlist(client, url, timeout=_IOS_WARM_TIMEOUT),
                timeout=_IOS_WARM_TIMEOUT)
        hls.remember_playlist_text(key, text, base, meta)
        hls.remember_playlist_meta(key, meta)
        return text, base, meta

    task = asyncio.create_task(run())
    _IOS_WARM[key] = task

    def _clean(done):
        if _IOS_WARM.get(key) is done:
            _IOS_WARM.pop(key, None)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(_clean)
    return task


def _prewarm_online(request: Request, brief: dict, guid: str,
                    index: int, line: int) -> None:
    """详情/取流接口就启动清单预热，缩短起播等待（失败忽略，不影响接口）。"""
    try:
        if hls.get_playlist_text((guid, line, index)):
            return
        picked = _pick(brief, line, index)
        if not picked or _is_direct_media(picked[0]):
            return
        _warm_playlist_task(request, brief, guid, index, line)
    except Exception:
        pass


# 红果直链（/api/stream）取址在服务端是「打上游签名接口 + 解密首块」，冷门集
# 第一次要 10–30 秒；播放器等不到就一直转圈。起播接口下发地址时顺手取 1KB
# 把地址与首块灌进源站缓存，播放器真正请求时秒回。
_HG_WARM: dict[str, float] = {}
_HG_WARM_TTL = 120.0


def _prewarm_hongguo(client, url: str, next_url: str = "") -> None:
    """后台取 1KB 触发源站取址（fire-and-forget，失败只记日志）。

    ``next_url`` 是下一集：追剧时一起排进队列，切集时源站已有地址，
    不会再让播放器等一次冷取址。
    """
    links = [str(url or ""), str(next_url or "")]
    links = [link for link in links if hongguo.STREAM_MARK in link]
    if not links:
        return
    now = time.monotonic()
    for key, ts in list(_HG_WARM.items()):
        if now - ts > _HG_WARM_TTL:
            _HG_WARM.pop(key, None)
    todo = [link for link in links
            if now - _HG_WARM.get(link, 0.0) >= _HG_WARM_TTL]
    if not todo:
        return
    for link in todo:
        _HG_WARM[link] = now

    async def fetch(link: str) -> None:
        req = client.build_request(
            "GET", link, headers={"User-Agent": hls._UA,
                                  "Range": "bytes=0-1023"})
        req.extensions["timeout"] = {"connect": 8.0,
                                     "read": hls._MEDIA_READ_TIMEOUT,
                                     "write": 30.0, "pool": 15.0}
        resp = await client.send(req, stream=True)
        try:
            await resp.aread()
        finally:
            await resp.aclose()

    async def run():
        for link in todo:
            try:
                await fetch(link)
            except Exception as exc:  # noqa: BLE001 - 预热失败不影响起播
                runtime.log(f"hongguo prewarm: {type(exc).__name__}")

    try:
        asyncio.create_task(run())
    except RuntimeError:  # 没有事件循环（测试/同步上下文）时直接跳过
        pass


@app.get(f"{ONLINE}/{{mg}}/main.m3u8")
async def main_m3u8(mg: str, request: Request):
    target = await _media_target(request, mg, enforce=False)
    if not target:
        runtime.log(f"main miss: {mg[:12]}")
        return Response(status_code=404)
    guid, index, brief = target
    try:
        line = int(request.query_params.get("line", 0))
    except ValueError:
        line = 0
    try:
        start_offset = float(request.query_params.get("start") or 0)
    except ValueError:
        start_offset = 0.0
    # iOS 1.13 对 EXT-X-START 兼容有问题（续播拿到就卡死）：start>0 时改用
    # 裁剪清单实现续播；start=0 时裁剪是空操作，起播清单与原来完全一致。
    slice_start = _is_ios(request)
    # 带显式 line 的 auto=1 来自 media/range（App 起播）：直接用该线路，
    # 不再竞速所有候选。竞速会给慢源叠加最多 3s，也可能选中“清单快但分片死”
    # 的线路，表现就是部分影片一直加载。
    auto_start = (request.query_params.get("auto") == "1"
                  and not request.query_params.get("line"))
    if not auto_start:
        cached = hls.get_playlist_text((guid, line, index))
        if cached:
            text, base, meta = cached
            return hls.playlist_response(text, base, SEG_PREFIX,
                                         f"{ONLINE}/variant",
                                         hls.is_master(text),
                                         hls.can_decrypt(),
                                         start_offset=start_offset,
                                         short_segs=_is_ios(request),
                                         slice_start=slice_start)
    if auto_start and config.get().get("auto_best_source", True):
        winner = await _race_lines(request.app.state.src, brief, guid, index,
                                   line, 3.0)
        if winner:
            attempt, text, base, meta = winner
            hls.remember_playlist_text((guid, attempt, index), text, base, meta)
            hls.remember_playlist_meta((guid, attempt, index), meta)
            hls.remember_playlist_meta((guid, line, index), meta)
            if attempt != line:
                runtime.log(f"prefer: {brief.get('title', '')[:40]} "
                            f"line {line} -> {attempt}")
            return hls.playlist_response(text, base, SEG_PREFIX,
                                         f"{ONLINE}/variant",
                                         hls.is_master(text),
                                         hls.can_decrypt(),
                                         start_offset=start_offset,
                                         short_segs=_is_ios(request),
                                         slice_start=slice_start)
    if request.query_params.get("ph") == "1":
        # iOS media/range 专用：绝不下发占位清单——1.13 的 fncache 会把这份
        # 直播式假清单缓存成该条目的播放会话，之后整部片只转圈（实测）。
        # 慢源多等几秒拿真实点播清单；真拿不到就落入下方换线兜底。
        task = _warm_playlist_task(request, brief, guid, index, line)
        if task is not None:
            try:
                text, base, meta = await asyncio.wait_for(
                    asyncio.shield(task), _IOS_WARM_TIMEOUT + 1.0)
            except asyncio.TimeoutError:
                runtime.log(f"ios slow playlist: {brief.get('title', '')[:40]} "
                            f"line{line} -> failover")
            except Exception as exc:
                _mark_bad_line(guid, line)
                runtime.log(f"ios playlist error: {brief.get('title', '')[:40]} "
                            f"line{line} ({type(exc).__name__})")
            else:
                return hls.playlist_response(text, base, SEG_PREFIX,
                                             f"{ONLINE}/variant",
                                             hls.is_master(text),
                                             hls.can_decrypt(),
                                             start_offset=start_offset,
                                             slice_start=slice_start)

    last_error = ""
    tried = set()
    while len(tried) < 2:
        bad = _bad_lines(guid) | tried
        attempts = [a for a in _ordered_lines(brief, line, bad) if a not in tried]
        if not attempts:
            break
        for attempt in attempts[:1]:
            tried.add(attempt)
            picked = _pick(brief, attempt, index)
            if not picked:
                continue
            url, _ = picked
            try:
                url = hls.ensure_public_target(url)
            except ValueError:
                _mark_bad_line(guid, attempt)
                last_error = "blocked"
                continue
            try:
                fetch_timeout = min(
                    float(config.get().get("play_timeout", 8.0)), 2.5)
                if _is_ios(request):
                    # iOS 不再喂占位清单，慢源（分享页要解一次 HTML）多给时间。
                    fetch_timeout = float(config.get().get("play_timeout", 8.0))
                text, base, meta = await hls.fetch_playlist(
                    request.app.state.src, url, timeout=fetch_timeout)
            except Exception as exc:
                last_error = type(exc).__name__
                _mark_bad_line(guid, attempt)
                continue
            hls.remember_playlist_text((guid, attempt, index), text, base, meta)
            hls.remember_playlist_meta((guid, attempt, index), meta)
            hls.remember_playlist_meta((guid, line, index), meta)
            if attempt != line:
                runtime.log(f"failover: {brief.get('title', '')[:40]} "
                            f"line {line} -> {attempt} ({last_error})")
            return hls.playlist_response(text, base, SEG_PREFIX,
                                         f"{ONLINE}/variant",
                                         hls.is_master(text),
                                         hls.can_decrypt(),
                                         start_offset=start_offset,
                                         short_segs=_is_ios(request),
                                         slice_start=slice_start)
    if last_error:
        runtime.log(f"playback failed: {brief.get('title', '')[:40]} ({last_error})")
    return Response(status_code=502)


@app.get(f"{ONLINE}/variant")
async def variant_playlist(request: Request):
    token = request.query_params.get("u", "")
    try:
        url = hls.ensure_public_target(hls.decode_url(token))
    except (ValueError, UnicodeDecodeError):
        return Response(status_code=400)
    try:
        text, base, meta = await hls.fetch_playlist(
            request.app.state.src, url,
            timeout=float(config.get().get("play_timeout", 8.0)), depth=1)
    except Exception as exc:
        runtime.log(f"variant error: {type(exc).__name__}")
        return Response(status_code=502)
    if hls.is_master(text):
        return Response(status_code=502)
    try:
        start_offset = float(request.query_params.get("start") or 0)
    except ValueError:
        start_offset = 0.0
    return hls.playlist_response(text, base, SEG_PREFIX,
                                 decrypt_keys=hls.can_decrypt(),
                                 start_offset=start_offset,
                                 short_segs=_is_ios(request),
                                 slice_start=_is_ios(request))


@app.get(f"{ONLINE}/{{mg}}/direct")
async def direct_media(mg: str, request: Request):
    target = await _media_target(request, mg, enforce=False)
    _dump_play("direct", {"mg": mg,
                          "line": request.query_params.get("line"),
                          "index": request.query_params.get("index")},
               {"index": target[1]} if target else None)
    if not target:
        return Response(status_code=404)
    guid, index, brief = target
    # 搜索结果/榜单里的红果条目可能只带 vod_id，没有分集线路：这里补一次
    # 详情，否则 _pick 拿不到地址，切第二集就变成 404（播放器一直转圈）。
    brief = await _online_brief(request, brief) or brief
    try:
        line = int(request.query_params.get("line", 0))
    except ValueError:
        line = 0
    picked = _pick(brief, line, index)
    if not picked:
        runtime.log(f"direct: no pick {brief.get('title', '')[:24]} "
                    f"ep{index + 1} line{line}")
        return Response(status_code=404)
    try:
        url = hls.ensure_public_target(picked[0])
    except ValueError:
        return Response(status_code=400)
    # 红果：高清档被限流（503/空响应）时换 compat（360P）重取一次，
    # 宁可降档也别让播放器无限转圈。
    retry = ()
    if hongguo.STREAM_MARK in url:
        alt = hongguo.compat_url(url)
        retry = (alt,) if alt else ()
    started = time.monotonic()
    resp = await hls.proxy_media(request, url, request.app.state.src,
                                 timeout=hls._MEDIA_READ_TIMEOUT,
                                 on_headers=hongguo.quality_hook(url),
                                 retry_urls=retry)
    runtime.log(f"direct: {brief.get('title', '')[:24]} ep{index + 1} "
                f"line{line} {resp.status_code} "
                f"{time.monotonic() - started:.1f}s")
    return resp


async def _serve_segment(token: str, request: Request):
    try:
        url, key_url, iv = hls.decode_seg(token)
    except Exception:  
        return Response(status_code=400)
    if url.split("?", 1)[0].lower().endswith((".key", "/key")):
        runtime.log(f"segment key: {url[:100]} ref={request.headers.get('referer', '')[:40]!r}")
    try:
        url = hls.ensure_public_target(url)
        if key_url:
            key_url = hls.ensure_public_target(key_url)
    except ValueError:
        return Response(status_code=400)
    # 非加密分片：能直连就 302，判定不过或曾直连失败都回退原来的代理逻辑。
    # 清单仍由本模块改写，因此广告过滤、线路优选、续播全部保持不变。
    if not key_url:
        direct = await hls.redirect_target(
            token, url, request.app.state.src, acl_on=acl.enabled())
        if direct:
            return Response(status_code=302, headers={
                "Location": direct,
                "Cache-Control": "no-store",
            })
    if key_url:
        try:
            key = await hls.fetch_key(request.app.state.src, key_url)
            data = await hls.fetch_media_bytes(request.app.state.src, url)
            plain = hls.decrypt_segment(data, key, iv)
        except Exception as exc:
            runtime.log(f"decrypt error: {type(exc).__name__}")
            plain = None
        if plain is not None:
            return Response(plain, media_type="video/mp2t",
                            headers={"Cache-Control": "public, max-age=3600"})
        runtime.log(f"decrypt failed: {url[:100]}")
    return await hls.proxy_media(request, url, request.app.state.src)


@app.get(f"{ONLINE}/seg")
async def segment(request: Request):
    return await _serve_segment(request.query_params.get("u", ""), request)


@app.get(f"{ONLINE}/seg/{{sid}}.ts")
async def segment_short(sid: str, request: Request):
    token = hls.get_seg_id(sid)
    if not token:
        return Response(status_code=404)
    return await _serve_segment(token, request)


async def _serve_online_poster(tok_path: str, request: Request) -> Response:
    tok = tok_path.lstrip("/")
    if "online/" in tok:
        tok = tok.split("online/", 1)[1]
    if "." in tok:
        tok = tok.rsplit(".", 1)[0]
    try:
        url = hls.ensure_public_target(hls.decode_url(tok))
    except ValueError:
        return Response(status_code=400)
    resp = await hls.proxy_poster(url, request.app.state.poster)
    if resp.media_type == "image/svg+xml":
        runtime.log(f"poster fallback: {url[:120]}")
    return resp


@app.get("/online/{tok_path:path}")
async def online_poster_short(tok_path: str, request: Request):
    return await _serve_online_poster(tok_path, request)


@app.get("/v/api/v1/sys/img/online/{tok_path:path}")
async def online_poster(tok_path: str, request: Request):
    return await _serve_online_poster(tok_path, request)


@app.get("/v/api/v1/sys/img/{tok_path:path}")
async def sys_img_any(tok_path: str, request: Request):
    # 客户端拼前缀时可能出现 “sys/img//online/...”，统一在这里兜住。
    if "online/" in tok_path:
        return await _serve_online_poster(tok_path, request)
    return await _passthrough(request)


@app.get(f"{ONLINE}/poster/{{token}}")
async def poster(token: str, request: Request):
    try:
        url = hls.ensure_public_target(hls.decode_url(token.rsplit(".", 1)[0]))
    except ValueError:
        return Response(status_code=400)
    return await hls.proxy_poster(url, request.app.state.poster)


@app.post("/v/api/v1/media/p")
async def media_p(request: Request):
    """播放器换清晰度/退出时的控制请求，官方要求 data.result=succ。"""
    body = await _body(request)
    link = body.get("playLink") or body.get("play_link") or ""
    req = str(body.get("req") or "")
    try:
        path = urlsplit(str(link)).path if isinstance(link, str) else ""
    except ValueError:
        path = ""
    if path.startswith(ONLINE + "/"):
        quality = body.get("quality") if isinstance(body.get("quality"), dict) else {}
        runtime.log(f"media/p: req={req!r} bitrate={quality.get('bitrate')!r} "
                    f"link={str(link)[:80]}")
        return JSONResponse({"code": 0, "msg": "", "data": {
            "reqid": body.get("reqid", ""), "result": "succ"}})
    return await _passthrough(request)


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE",
                                              "PATCH", "HEAD", "OPTIONS"])
async def catch_all(full_path: str, request: Request):
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        try:
            parsed = json.loads(await request.body() or b"{}")
            guid = str(parsed.get("guid") or parsed.get("item_guid") or "") \
                if isinstance(parsed, dict) else ""
            if not guid:
                for part in reversed(full_path.split("/")):
                    if len(part) == 32 and all(ch in "0123456789abcdef" for ch in part):
                        guid = part
                        break
            store = request.app.state.store
            if guid and (_brief(store, guid) or store.resolve_episode(guid)):
                runtime.log(f"virtual noop {request.method} {full_path} "
                            f"keys={list(parsed)[:8]} "
                            f"bitrate={parsed.get('bitrate')!r}")
                return JSONResponse({"code": 0, "msg": "", "data": {}})
        except (ValueError, AttributeError):
            pass
    return await _passthrough_stream(request)
