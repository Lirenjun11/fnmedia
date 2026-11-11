#!/usr/bin/env python3
from __future__ import annotations

import asyncio
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
import borrow
import cms
import config
import douban
import fake
import hls
import playstate
import playstate_api
import runtime
import upstream

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
    runtime.log(f"service started, sources={len(config.enabled_sources())}")
    worker = asyncio.create_task(_auto_update_worker())
    try:
        yield
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        playstate.persist(force=True)
        await app.state.up.aclose()
        await app.state.src.aclose()
        await app.state.poster.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url="/v/api/ext/openapi.json")


@app.middleware("http")
async def access_log(request: Request, call_next):
    started = time.monotonic()
    try:
        resp = await call_next(request)
    except Exception as exc:  
        runtime.log(f"ERROR {request.method} {request.url.path} {type(exc).__name__}")
        raise
    path = request.url.path
    if path.endswith("/task/running") or "/sys/img/" in path or \
            path.startswith("/v/api/ext/logs"):
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


def _err(msg: str, status: int = 404) -> JSONResponse:
    return JSONResponse({"code": -1, "msg": msg, "data": None}, status_code=status)


@app.get("/_ext/healthz")
async def healthz():
    return {"ok": True, "service": "fnos_media_ext",
            "sources": len(config.enabled_sources())}


@app.get("/v/online-admin")
async def admin_page():
    index = STATIC / "index.html"
    if not index.exists():
        return HTMLResponse("<!doctype html><meta charset='utf-8'>"
                            "<h1>fnos_media_ext</h1><p>前端产物未构建。</p>")
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


@app.get("/v/api/v1/search/list")
async def search_list(request: Request):
    local = await _passthrough(request)
    sources = config.enabled_sources()
    keyword = request.query_params.get("q", "").strip()
    if not sources or not keyword:
        return local
    try:
        data = json.loads(local.body)
    except (ValueError, AttributeError):
        return local
    if not isinstance(data, dict):
        return local
    items = data.get("data") if isinstance(data.get("data"), list) else []
    cfg = config.get()
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
    ep = store.resolve_episode(guid)
    if ep:
        brief = store.get(ep[0])
        if not brief:
            return await _passthrough(request)
        row = playstate.apply(fake.episode_item(brief, ep[1]), guid)
        return JSONResponse(fake.ok(fake.normalize_detail(row, _ua(request))))
    tv_guid = store.resolve_season(guid)
    if tv_guid:
        brief = store.get(tv_guid)
        if not brief:
            return await _passthrough(request)
        row = playstate.apply(fake.season_item(brief), guid)
        return JSONResponse(fake.ok(fake.normalize_detail(row, _ua(request))))
    if not store.get(guid):
        resolved = store.resolve_media(guid)
        if resolved:
            tv_guid, index = resolved
            brief = store.get(tv_guid)
            if brief:
                if guid in (fake.media_guid(tv_guid), fake.video_guid(tv_guid)):
                    return await _item_title_detail(brief, tv_guid, request)
                row = playstate.apply(fake.episode_item(brief, index), guid)
                return JSONResponse(fake.ok(
                    fake.normalize_detail(row, _ua(request))))
    brief = store.get(guid)
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
    detail = playstate.apply(detail, guid)
    return JSONResponse(fake.ok(fake.normalize_detail(detail, _ua(request))))


@app.get("/v/api/v1/season/list/{guid}")
async def season_route(guid: str, request: Request):
    store = request.app.state.store
    tv_guid = _tv_guid_of(store, guid)
    brief = store.get(tv_guid) if tv_guid else None
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
    return JSONResponse(fake.ok(fake.normalize_cards(rows, ua)))


@app.post("/v/api/v1/season/list/{guid}")
async def season_route_post(guid: str, request: Request):
    return await season_route(guid, request)


@app.get("/v/api/v1/episode/list/{guid}")
async def episode_route(guid: str, request: Request):
    store = request.app.state.store
    tv_guid = _tv_guid_of(store, guid)
    brief = store.get(tv_guid) if tv_guid else None
    if not brief:
        return await _passthrough(request)
    return JSONResponse(fake.ok(fake.normalize_cards(
        fake.episode_list(brief), _ua(request))))


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
    parent = str(body.get("parent_guid") or body.get("ancestor_guid") or "")
    if not parent:
        return await _passthrough(request)
    brief = store.get(parent)
    if brief and brief.get("is_tv"):
        rows = fake.normalize_cards(fake.season_list(brief), _ua(request))
        return JSONResponse(fake.ok(_listing(rows)))
    tv_guid = store.resolve_season(parent)
    brief = store.get(tv_guid) if tv_guid else None
    if brief:
        rows = fake.normalize_cards(fake.episode_list(brief), _ua(request))
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
    ep = store.resolve_episode(guid)
    if ep:
        tv_guid, _ = ep
        mg, vg = fake.ep_media_guid(guid), fake.ep_video_guid(guid)
    elif store.get(guid):
        tv_guid = guid
        mg, vg = fake.media_guid(guid), fake.video_guid(guid)
    else:
        return await _passthrough(request)
    brief = store.get(tv_guid)
    if not brief:
        return await _passthrough(request)
    ep_index = ep[1] if ep else 0
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
    return guid if store.get(guid) else None


def _identity(store: cms.TitleStore, guid: str) -> tuple[str, str, str] | None:
    ep = store.resolve_episode(guid)
    if ep:
        return guid, fake.ep_media_guid(guid), fake.ep_video_guid(guid)
    if store.get(guid):
        return guid, fake.media_guid(guid), fake.video_guid(guid)
    return None


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
    wanted = str(body.get("line") or "")
    if wanted:
        found = _clamp_line(wanted, names, brief)
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
    found = fake.line_index(body.get("bitrate"), names)
    return found if found is not None else 0


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
    if params.get("line"):
        found = _clamp_line(params["line"], names, brief)
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
    found = fake.line_index(params.get("bitrate"), names)
    if found is not None:
        return found, True
    return 0, False


@app.post("/v/api/v1/play/info")
async def play_info_route(request: Request):
    body = await _body(request)
    guid = str(body.get("item_guid") or "")
    store = request.app.state.store
    identity = _identity(store, guid)
    if not identity:
        return await _passthrough(request)
    _, mg, vg = identity
    ep = store.resolve_episode(guid)
    if ep:
        brief = store.get(ep[0])
        if not brief:
            return await _passthrough(request)
        info = fake.episode_play_info(brief, ep[1], guid)
        canonical = ep[0]
    else:
        brief = store.get(guid)
        if not brief:
            return await _passthrough(request)
        info = fake.play_info(brief, mg, vg, guid)
        canonical = guid
    info["ts"] = playstate.ts_for(canonical)
    return JSONResponse(fake.ok(fake.normalize_detail(info, _ua(request))))


@app.post("/v/api/v1/stream")
async def stream_post(request: Request):
    body = await _body(request)
    mg = str(body.get("media_guid") or body.get("video_guid")
             or body.get("guid") or "")
    store = request.app.state.store
    resolved = store.resolve_media(mg)
    if not resolved:
        return await _passthrough(request)
    guid, index = resolved
    brief = store.get(guid)
    if not brief:
        return await _passthrough(request)
    line = _line_from_body(brief, body)
    meta = hls.get_playlist_meta((guid, line, index))
    data = fake.stream_payload(
        brief, mg,
        str(body.get("video_guid") or _video_guid_for(guid, index, mg)),
        meta, line)
    runtime.log(f"stream: {brief.get('title', '')[:30]} ep{index + 1} "
                f"line={line}/{len(brief.get('line_names') or [])} "
                f"bitrate={body.get('bitrate')!r} ua={_ua(request)[:40]!r}")
    return JSONResponse(fake.ok(data))


@app.post("/v/api/v1/person/list/{guid}")
async def person_list_route(guid: str, request: Request):
    store = request.app.state.store
    target = _tv_guid_of(store, guid)
    brief = store.get(target) if target else None
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
    mg = file_hash
    resolved = store.resolve_media(file_hash)
    if not resolved:
        ep = store.resolve_episode(file_hash)
        if not ep:
            return await _passthrough(request)
        resolved = ep
        mg = fake.ep_media_guid(file_hash)
    guid, index = resolved
    brief = store.get(guid)
    if not brief:
        return await _passthrough(request)
    line, explicit = _range_selection(request, brief)
    picked = _pick(brief, line, index)
    if not picked:
        return Response(status_code=404)
    url = picked[0]
    is_hls = ".m3u8" in url.lower()
    runtime.log(f"media/range takeover: {brief.get('title', '')[:30]} "
                f"ep{index + 1} line{line} -> {'hls' if is_hls else 'direct'}")
    if is_hls:
        auto = "" if explicit else "&auto=1"
        return Response(status_code=302, headers={
            "Location": f"{ONLINE}/{mg}/preset.m3u8?line={line}"
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
    mg = str(body.get("media_guid") or body.get("video_guid")
             or body.get("guid") or "")
    store = request.app.state.store
    resolved = store.resolve_media(mg)
    if not resolved:
        return await _passthrough(request)
    guid, index = resolved
    brief = store.get(guid)
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
    is_hls = ".m3u8" in url.lower()
    suffix = "preset.m3u8" if is_hls else "direct"
    link = f"{ONLINE}/{mg}/{suffix}?line={line}&index={index}"
    if not explicit:
        link += "&auto=1"
    runtime.log(f"play: {brief.get('title', '')[:30]} ep{index + 1} "
                f"line={line}/{len(brief.get('line_names') or [])} "
                f"bitrate={body.get('bitrate')!r} auto={not explicit} "
                f"ua={_ua(request)[:40]!r}")
    return JSONResponse(fake.play_response(
        mg, str(body.get("video_guid") or _video_guid_for(guid, index, mg)),
        link, "HLS" if is_hls else "MP4"))


@app.get(f"{ONLINE}/{{mg}}/preset.m3u8")
async def preset_m3u8(mg: str, request: Request):
    """播放入口清单：官方播放器要求 play_link 是 master，哪怕只有一条变体。"""
    resolved = request.app.state.store.resolve_media(mg)
    if not resolved:
        return Response(status_code=404)
    guid, index = resolved
    brief = request.app.state.store.get(guid)
    if not brief:
        return Response(status_code=404)
    line, _ = _range_selection(request, brief)
    names = brief.get("line_names") or []
    # 只给当前线路一条变体：手机端会按 master 变体顺序串行预取每一条
    # main.m3u8，多线路会把起播拖过播放器超时；换线由 qualities 触发的
    # play/play 重新下发单变体 master 完成。
    label = (fake.line_display(brief, names[line]) if names
             else "原画").replace('"', "")
    auto = "&auto=1" if request.query_params.get("auto") == "1" else ""
    text = ("#EXTM3U\n#EXT-X-VERSION:3\n"
            f'#EXT-X-STREAM-INF:BANDWIDTH=2000000,PROGRAM-ID=1,NAME="{label}"\n'
            f"main.m3u8?line={line}&index={index}{auto}\n")
    return Response(text, media_type="application/vnd.apple.mpegurl",
                    headers={"Access-Control-Allow-Origin": "*",
                             "Cache-Control": "no-store"})


@app.get(f"{ONLINE}/{{mg}}/main.m3u8")
async def main_m3u8(mg: str, request: Request):
    resolved = request.app.state.store.resolve_media(mg)
    if not resolved:
        return Response(status_code=404)
    guid, index = resolved
    brief = request.app.state.store.get(guid)
    if not brief:
        return Response(status_code=404)
    try:
        line = int(request.query_params.get("line", 0))
    except ValueError:
        line = 0
    auto_start = request.query_params.get("auto") == "1"
    if auto_start and config.get().get("auto_best_source", True):
        winner = await _race_lines(request.app.state.src, brief, guid, index,
                                   line, 3.0)
        if winner:
            attempt, text, base, meta = winner
            hls.remember_playlist_meta((guid, attempt, index), meta)
            hls.remember_playlist_meta((guid, line, index), meta)
            if attempt != line:
                runtime.log(f"prefer: {brief.get('title', '')[:40]} "
                            f"line {line} -> {attempt}")
            return hls.playlist_response(text, base, SEG_PREFIX,
                                         f"{ONLINE}/variant",
                                         hls.is_master(text))
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
            hls.remember_playlist_meta((guid, attempt, index), meta)
            hls.remember_playlist_meta((guid, line, index), meta)
            if attempt != line:
                runtime.log(f"failover: {brief.get('title', '')[:40]} "
                            f"line {line} -> {attempt} ({last_error})")
            return hls.playlist_response(text, base, SEG_PREFIX,
                                         f"{ONLINE}/variant",
                                         hls.is_master(text))
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
    return hls.playlist_response(text, base, SEG_PREFIX)


@app.get(f"{ONLINE}/{{mg}}/direct")
async def direct_media(mg: str, request: Request):
    resolved = request.app.state.store.resolve_media(mg)
    if not resolved:
        return Response(status_code=404)
    guid, index = resolved
    brief = request.app.state.store.get(guid)
    if not brief:
        return Response(status_code=404)
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
        url = hls.decode_url(token)
    except Exception:  
        return Response(status_code=400)
    try:
        url = hls.ensure_public_target(url)
    except ValueError:
        return Response(status_code=400)
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
            if guid and (store.get(guid) or store.resolve_episode(guid)):
                runtime.log(f"virtual noop {request.method} {full_path} "
                            f"keys={list(parsed)[:8]} "
                            f"bitrate={parsed.get('bitrate')!r}")
                return JSONResponse({"code": 0, "msg": "", "data": {}})
        except (ValueError, AttributeError):
            pass
    return await _passthrough_stream(request)
