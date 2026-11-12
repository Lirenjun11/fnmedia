#!/usr/bin/env python3
"""虚拟媒体库的官方接口接管：media library list / mdb detail / sum。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import vlib

router = APIRouter()


async def _passthrough(request: Request):
    import app as app_module
    return await app_module._passthrough(request)


def _payload(resp) -> dict | None:
    try:
        data = json.loads(resp.body)
    except (ValueError, AttributeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


@router.get("/v/api/v1/mediadb/list")
async def mediadb_list(request: Request):
    upstream = await _passthrough(request)
    if not vlib.enabled():
        return upstream
    payload = _payload(upstream)
    libs = vlib.libraries()
    if payload is None:
        # 官方返回非法（升级/异常）：有虚拟库就用它兜底，避免整页报错。
        return JSONResponse({"code": 0, "msg": "", "data": libs}) if libs \
            else upstream
    if payload.get("code") != 0:
        if not libs:
            return upstream
        payload["code"] = 0
        payload["msg"] = ""
        payload["data"] = libs
        return JSONResponse(payload)
    local = payload.get("data")
    if not isinstance(local, list):
        return upstream
    ours = vlib.lib_guids()
    local = [row for row in local if row.get("guid") not in ours]
    payload["data"] = libs + local
    return JSONResponse(payload)


@router.get("/v/api/v1/mdb/{guid}")
async def mdb_detail(guid: str, request: Request):
    detail = vlib.library_detail(guid)
    if detail is None:
        return await _passthrough(request)
    return JSONResponse({"code": 0, "msg": "", "data": detail})


@router.get("/v/api/v1/mediadb/sum")
async def mediadb_sum(request: Request):
    upstream = await _passthrough(request)
    if not vlib.enabled():
        return upstream
    payload = _payload(upstream)
    counts = vlib.sum_counts()
    if payload is None or not isinstance(payload.get("data"), dict):
        if counts:
            return JSONResponse({"code": 0, "msg": "", "data": counts})
        return upstream
    data = payload["data"]
    added = 0
    for guid, number in counts.items():
        data[guid] = number
        added += number
    if added:
        try:
            data["total"] = int(data.get("total") or 0) + added
        except (TypeError, ValueError):
            data["total"] = added
    return JSONResponse(payload)
