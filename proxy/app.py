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
import catalog as catalog_mod
import charts
import cms
import config
import douban
import fake
import hls
import playstate
import playstate_api
import runtime
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
                        and (catalog_mod.catalog.stale()
                             or (vlib.split_by_source()
                                 and catalog_mod.catalog.legacy_routes())):
                    # 从 0.0.1.19 升级时旧路由是 <sid>:movie，按源拆库后
                    # 查 <sid>:<type_id> 会全部落空，启动时强制重建一次。
                    await catalog_mod.catalog.refresh_routes(
                        app.state.src,
                        vlib.mapped_sources() or config.enabled_sources(),
                        split=vlib.split_by_source())
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
    playstate.load()
    vlib.bind_client(app.state.src)
    restored = vlib.restore(app.state.store)
    runtime.log(f"service started, sources={len(config.enabled_sources())} "
                f"aes_decrypt={'on' if hls.can_decrypt() else 'off'} "
                f"vlib_charts={charts.chart_cache.total()} restored={restored}")
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
    local = await _passthrough(request)
    sources = acl.sources()
    keyword = request.query_params.get("q", "").strip()
    if not sources or not keyword:
        return local
    cfg = config.get()
    debounce_ms = int(cfg.get("search_debounce_ms", 200))
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
            concurrency=int(cfg.get("search_concurrency", 5)),
        )
    except Exception as exc:  
        runtime.log(f"search error: {type(exc).__name__}")
        titles = []
    if cfg.get("merge_sources", True):
        titles = cms.merge_titles(titles)
    briefs = request.app.state.store.remember(titles)
    cards = fake.normalize_cards(
        [playstate.apply(fake.search_card(b), b["guid"]) for b in briefs],
        _ua(request))
    data["data"] = cards + items if cfg.get("online_first", True) else items + cards
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
    detail = fake.detail_item(brief)
    borrowed = False
    if config.get().get("borrow_official", True):
        try:
            detail = borrow.enrich_detail(detail, brief.get("title"),
                                          brief.get("year"))
            borrowed = bool(detail.pop("_borrowed", False))
        except Exception as exc:
            runtime.log(f"borrow error: {type(exc).__name__}")
    if config.get().get("douban_enabled") and not borrowed:
        try:
            detail = await douban.enrich_poster(
                request.app.state.poster, detail,
                brief.get("title"), brief.get("year"))
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
    elif _brief(store, guid):
        tv_guid = guid
        brief = _brief(store, tv_guid)
        # 电视剧整剧身份 -> 续播集，和 play/info 保持一致。
        ep_index = _resume_index(store, tv_guid) if brief and brief.get("is_tv") else 0
        eg = fake.episode_guid(tv_guid, ep_index) if brief and brief.get("is_tv") else ""
    else:
        return await _passthrough(request)
    brief = _brief(store, tv_guid)
    if not brief:
        return await _passthrough(request)
    if ep:
        mg, vg = fake.ep_media_guid(guid), fake.ep_video_guid(guid)
    elif eg:
        mg, vg = fake.ep_media_guid(eg), fake.ep_video_guid(eg)
    else:
        mg, vg = fake.media_guid(guid), fake.video_guid(guid)
    meta = hls.get_playlist_meta((tv_guid, 0, ep_index))
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


@app.post("/v/api/v1/play/info")
async def play_info_route(request: Request):
    body = await _body(request)
    store = request.app.state.store
    ref = await _resolve_play_ref(store, request.app.state.src, body)
    if not ref:
        runtime.log(f"play/info unresolved keys={sorted(body)}")
        return await _passthrough(request)
    guid, mg, vg = ref["guid"], ref["media"], ref["video"]
    if ref["episode"]:
        brief = _brief(store, guid)
        if not brief:
            return await _passthrough(request)
        index = ref["index"]
        info = fake.episode_play_info(brief, index, ref["episode"])
        canonical = guid
    else:
        brief = _brief(store, guid)
        if not brief:
            return await _passthrough(request)
        if brief.get("is_tv"):
            # 整部剧点播放：解析到续播集，播放器才有“下一集/选集”身份。
            index = _resume_index(store, guid)
            eg = fake.episode_guid(guid, index)
            info = fake.episode_play_info(brief, index, eg)
        else:
            index = 0
            info = fake.play_info(brief, mg, vg, guid)
        canonical = guid
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
    return JSONResponse(fake.ok(fake.normalize_detail(info, _ua(request))))


@app.post("/v/api/v1/stream")
async def stream_post(request: Request):
    body = await _body(request)
    store = request.app.state.store
    ref = await _resolve_play_ref(store, request.app.state.src, body)
    if not ref:
        runtime.log(f"stream unresolved keys={sorted(body)}")
        return await _passthrough(request)
    guid, index, mg = ref["guid"], ref["index"], ref["media"]
    brief = _brief(store, guid)
    if not brief:
        return await _passthrough(request)
    line = _line_from_body(brief, body)
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
    if not ref:
        return await _passthrough(request)
    guid, index, mg = ref["guid"], ref["index"], ref["media"]
    brief = _brief(store, guid)
    if not brief:
        return await _passthrough(request)
    line, explicit = _range_selection(request, brief)
    picked = _pick(brief, line, index)
    if not picked:
        return Response(status_code=404)
    url = picked[0]
    is_hls = not _is_direct_media(url)
    runtime.log(f"media/range takeover: {brief.get('title', '')[:30]} "
                f"ep{index + 1} line{line} -> {'hls' if is_hls else 'direct'}")
    if is_hls:
        # 原生播放器走 Range：直接给媒体清单，避免多包一层 master 等待。
        auto = "&auto=1" if not explicit else ""
        return Response(status_code=302, headers={
            "Location": f"{ONLINE}/{mg}/main.m3u8?line={line}"
                        f"&index={index}{auto}"})
    try:
        url = hls.ensure_public_target(url)
    except ValueError:
        _mark_bad_line(guid, line)
        return Response(status_code=404)
    return await hls.proxy_media(request, url, request.app.state.src)


@app.post("/v/api/v1/play/play")
async def play_play(request: Request):
    body = await _body(request)
    store = request.app.state.store
    ref = await _resolve_play_ref(store, request.app.state.src, body)
    if not ref:
        runtime.log(f"play/play unresolved keys={sorted(body)}")
        return await _passthrough(request)
    guid, index, mg = ref["guid"], ref["index"], ref["media"]
    brief = _brief(store, guid)
    if not brief:
        return await _passthrough(request)
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
    is_hls = not _is_direct_media(url)
    suffix = "preset.m3u8" if is_hls else "direct"
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


async def _warm_line(client, brief: dict, guid: str, index: int,
                     no: int) -> tuple | None:
    key = (guid, no, index)
    hit = hls.get_playlist_text(key)
    if hit:
        return (no,) + hit
    picked = _pick(brief, no, index)
    if not picked or _is_direct_media(picked[0]):
        return None
    try:
        url = hls.ensure_public_target(picked[0])
        text, base, meta = await hls.fetch_playlist(
            client, url, timeout=min(float(config.get().get("play_timeout", 8.0)),
                                     2.0))
    except Exception:
        return None
    hls.remember_playlist_text(key, text, base, meta)
    hls.remember_playlist_meta(key, meta)
    return no, text, base, meta


@app.get(f"{ONLINE}/{{mg}}/preset.m3u8")
async def preset_m3u8(mg: str, request: Request):
    """播放 master：只列当前线路。手机端会逐条预取 master 的所有变体，
    多变体会把它拖到超时；换线由 qualities -> play/play 重新下发单变体。"""
    target = await _media_target(request, mg, enforce=False)
    if not target:
        runtime.log(f"preset miss: {mg[:12]}")
        return Response(status_code=404)
    guid, index, brief = target
    line, _ = _range_selection(request, brief)
    names = brief.get("line_names") or []
    line = max(0, min(line, len(names) - 1)) if names else 0
    selected_pick = _pick(brief, line, index)
    if selected_pick and _is_direct_media(selected_pick[0]):
        return Response(status_code=302, headers={
            "Location": f"{ONLINE}/{mg}/direct?line={line}&index={index}"})

    # 当前线路先就绪并缓存，播放器随后取 main.m3u8 直接命中。
    selected = asyncio.create_task(
        _warm_line(request.app.state.src, brief, guid, index, line))
    try:
        await asyncio.wait_for(asyncio.shield(selected),
                               timeout=min(float(config.get().get(
                                   "play_timeout", 8.0)), 2.0))
    except asyncio.TimeoutError:
        pass
    label = (fake.line_display(brief, names[line]) if names
             else "原画").replace('"', "")
    main_uri = f"main.m3u8?line={line}&index={index}"
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
                             "Cache-Control": "no-store"})


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
    auto_start = request.query_params.get("auto") == "1"
    if not auto_start:
        cached = hls.get_playlist_text((guid, line, index))
        if cached:
            text, base, meta = cached
            return hls.playlist_response(text, base, SEG_PREFIX,
                                         f"{ONLINE}/variant",
                                         hls.is_master(text),
                                         hls.can_decrypt(),
                                         start_offset=start_offset)
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
                                         start_offset=start_offset)
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
                text, base, meta = await hls.fetch_playlist(
                    request.app.state.src, url,
                    timeout=min(float(config.get().get("play_timeout", 8.0)), 2.5))
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
                                         start_offset=start_offset)
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
                                 start_offset=start_offset)


@app.get(f"{ONLINE}/{{mg}}/direct")
async def direct_media(mg: str, request: Request):
    target = await _media_target(request, mg, enforce=False)
    if not target:
        return Response(status_code=404)
    guid, index, brief = target
    try:
        line = int(request.query_params.get("line", 0))
    except ValueError:
        line = 0
    picked = _pick(brief, line, index)
    if not picked:
        return Response(status_code=404)
    try:
        url = hls.ensure_public_target(picked[0])
    except ValueError:
        return Response(status_code=400)
    return await hls.proxy_media(request, url, request.app.state.src)


@app.get(f"{ONLINE}/seg")
async def segment(request: Request):
    token = request.query_params.get("u", "")
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
