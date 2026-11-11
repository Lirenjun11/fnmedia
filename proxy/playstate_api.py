"""Official playback-state endpoints for virtual online items."""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import fake
import hls
import playstate
import upstream

router = APIRouter()


async def _passthrough(request: Request):
    try:
        return await upstream.forward(request, request.app.state.up)
    except Exception:
        return JSONResponse({"code": -1, "msg": "official service unavailable",
                            "data": None}, status_code=502)


async def _body(request: Request) -> dict:
    try:
        value = json.loads(await request.body() or b"{}")
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _canonical(request: Request, guid: str):
    store = request.app.state.store
    ep = store.resolve_episode(guid)
    target = ep[0] if ep else guid
    return store.get(target), target


def _ua(request: Request) -> str:
    return request.headers.get("user-agent", "")


def _guid(body: dict) -> str:
    return str(body.get("guid") or body.get("item_guid") or "")


def _ok(guid: str) -> JSONResponse:
    return JSONResponse({"code": 0, "msg": "", "data": {"guid": guid}})


@router.put("/v/api/v1/item/favorite")
async def favorite_add(request: Request):
    body = await _body(request)
    guid = _guid(body)
    brief, canonical = _canonical(request, guid)
    if not brief:
        return await _passthrough(request)
    playstate.add_favorite(canonical, brief)
    return _ok(guid)


@router.delete("/v/api/v1/item/favorite")
async def favorite_del(request: Request):
    body = await _body(request)
    guid = _guid(body)
    brief, canonical = _canonical(request, guid)
    if not brief:
        return await _passthrough(request)
    playstate.remove_favorite(canonical)
    return _ok(guid)


@router.post("/v/api/v1/item/watched")
async def watched_set(request: Request):
    body = await _body(request)
    guid = _guid(body)
    brief, canonical = _canonical(request, guid)
    if not brief:
        return await _passthrough(request)
    try:
        want = int(body.get("watched", body.get("is_watched", 1)))
    except (TypeError, ValueError):
        want = 1
    if want == 0:
        playstate.remove_watched(canonical)
    else:
        playstate.set_watched(canonical)
    return _ok(guid)


@router.delete("/v/api/v1/item/watched")
async def watched_del(request: Request):
    body = await _body(request)
    guid = _guid(body)
    brief, canonical = _canonical(request, guid)
    if not brief:
        return await _passthrough(request)
    playstate.remove_watched(canonical)
    return _ok(guid)


@router.post("/v/api/v1/favorite/list")
async def favorite_list(request: Request):
    body = await _body(request)
    local = await _passthrough(request)
    try:
        payload = json.loads(local.body)
    except (ValueError, AttributeError):
        return local
    if not isinstance(payload, dict):
        return local
    bucket = payload.get("data")
    if not isinstance(bucket, dict):
        return local
    cards = []
    for guid in playstate.favorite_guids():
        brief = request.app.state.store.get(guid)
        if not brief:
            continue
        cards.append(playstate.apply(fake.search_card(brief), guid))
    cards = fake.normalize_cards(cards, _ua(request))
    official = bucket.get("list") or []
    merged = cards + list(official)
    try:
        page = max(1, int(body.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = max(1, int(body.get("page_size") or len(merged) or 50))
    except (TypeError, ValueError):
        page_size = max(1, len(merged) or 50)
    start = (page - 1) * page_size
    bucket["total"] = len(merged)
    bucket["list"] = merged[start:start + page_size]
    payload["code"] = 0
    payload["msg"] = ""
    return JSONResponse(payload)


@router.post("/v/api/v1/play/record")
async def play_record(request: Request):
    body = await _body(request)
    guid = _guid(body)
    brief, canonical = _canonical(request, guid)
    if not brief:
        return await _passthrough(request)
    meta = hls.get_playlist_meta((canonical, 0, 0)) or {}
    playstate.record({**body, "item_guid": canonical}, brief,
                     float(meta.get("duration") or 0))
    return JSONResponse({"code": 0, "msg": "", "data": True})


@router.delete("/v/api/v1/play/record")
async def play_record_del(request: Request):
    body = await _body(request)
    raw = body.get("item_guid") or body.get("guid") or []
    ids = raw if isinstance(raw, list) else [raw]
    online = []
    for guid in ids:
        brief, canonical = _canonical(request, str(guid or ""))
        if brief and canonical not in online:
            online.append(canonical)
    if not online:
        return await _passthrough(request)
    playstate.delete_records(online)
    return JSONResponse({"code": 0, "msg": "", "data": True})


@router.get("/v/api/v1/play/list")
async def play_list(request: Request):
    local = await _passthrough(request)
    try:
        local_items = json.loads(local.body).get("data") or []
    except (ValueError, AttributeError):
        return local
    if not isinstance(local_items, list):
        local_items = []
    cards = []
    for guid, rec in playstate.history_records():
        brief = request.app.state.store.get(guid)
        if not brief:
            continue
        cards.append(fake.history_card(brief, rec))
    cards.sort(key=lambda row: row.get("_updated_at", 0), reverse=True)
    for row in cards:
        row.pop("_updated_at", None)
    cards = fake.normalize_cards(cards, _ua(request))
    online_guids = {row.get("guid") for row in cards}
    local_only = [row for row in local_items
                  if isinstance(row, dict) and row.get("guid") not in online_guids]
    return JSONResponse({"code": 0, "msg": "", "data": cards + local_only},
                        headers={"Content-Type": "application/json; charset=utf-8"})


@router.get("/v/api/v1/media/itemfile/{guid}")
async def media_itemfile(guid: str, request: Request):
    brief, _ = _canonical(request, guid)
    if not brief:
        return await _passthrough(request)
    return JSONResponse({"code": 0, "msg": "", "data": {}})
