#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import cms
import catalog
import charts
import config
import hls
import runtime
import subscription
import upcheck
import vlib

router = APIRouter(prefix="/v/api/ext")
COOKIE = "fnos_media_admin"


def guard(request: Request) -> None:
    token = config.token()
    if (request.headers.get("x-admin-token") != token
            and request.cookies.get(COOKIE) != token):
        raise HTTPException(status_code=401, detail="未授权：请重新打开管理页")


def ok(data) -> dict:
    return {"code": 0, "msg": "", "data": data}


def fail(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"code": -1, "msg": msg, "data": None}, status_code=status)


async def body_json(request: Request) -> dict:
    try:
        raw = await request.body()
        value = json.loads(raw or b"{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _version() -> str:
    try:
        version_file = Path(__file__).resolve().parent.parent / "VERSION"
        return version_file.read_text(encoding="utf-8").strip() or "0.0.1.10"
    except OSError:
        return "0.0.1.10"


def _public_config() -> dict:
    cfg = config.get()
    cfg.pop("admin_token", None)
    return cfg


async def fetch_and_apply(url: str | None = None,
                          text: str | None = None) -> dict:
    cfg = config.get()
    source_url = (url or cfg.get("subscription_url") or "").strip()
    if text is None:
        if not source_url:
            raise ValueError("未配置订阅地址")
        text = await subscription.fetch(source_url)
    obj = subscription.decode_payload(text)
    items, report = subscription.normalize(obj)
    for item in items:
        item["enabled"] = not item.get("adult")
        item["from"] = "subscription"
    manual = [dict(s) for s in cfg.get("sources", [])
              if s.get("from") == "manual"]
    merged, stats = subscription.merge_all(items, manual)
    cfg["sources"] = merged
    cfg["config_text"] = json.dumps(obj, ensure_ascii=False, indent=2)
    cfg["last_update"] = int(time.time())
    if source_url:
        cfg["subscription_url"] = source_url
    config.save(cfg)
    enabled_count = sum(1 for item in items if item.get("enabled"))
    return {**report, **stats, "enabled": enabled_count, "url": source_url}


@router.get("/update/check")
async def update_check(request: Request):
    guard(request)
    result = await upcheck.check(
        config.get().get("update_repo"), _version(),
        force=request.query_params.get("force") == "1")
    return ok(result)


@router.get("/bootstrap")
async def bootstrap(request: Request):
    cfg = _public_config()
    sources = config.all_sources()
    enabled = [s for s in sources if s.get("enabled")]
    return ok({
        "version": _version(),
        "uptime": round(time.time() - runtime.STARTED_AT, 1),
        "online_enabled": bool(enabled),
        "sources_total": len(sources),
        "sources_enabled": len(enabled),
        "titles_cached": request.app.state.store.count(),
        "last_update": cfg.get("last_update", 0),
        "config": cfg,
        "sources": sources,
        "vlib": vlib_status(),
    })


@router.patch("/config")
async def patch_config(request: Request):
    guard(request)
    body = await body_json(request)
    return ok({"config": {k: v for k, v in config.patch(body).items()
                          if k != "admin_token"}})


@router.post("/sources")
async def add_source(request: Request):
    guard(request)
    body = await body_json(request)
    api = subscription.unwrap_proxy(str(body.get("api") or "").strip())
    name = str(body.get("name") or "").strip()[:60]
    if not api.startswith(("http://", "https://")):
        return fail("接口地址必须以 http(s) 开头")
    candidates, _ = subscription.normalize(
        {"sources": [{
            "name": name, "api": api,
            "_key": str(body.get("key") or "").strip(),
            "detail": str(body.get("detail") or "").strip(),
        }]}, origin="manual")
    if not candidates:
        return fail("不是可识别的苹果 CMS 采集接口")
    cfg = config.get()
    if any(s.get("id") == candidates[0]["id"] for s in cfg["sources"]):
        return fail("该接口已存在")
    item = candidates[0]
    item["enabled"] = bool(body.get("enabled", False))
    cfg["sources"].append(item)
    config.save(cfg)
    return ok({"source": item})


@router.patch("/sources/{sid}")
async def update_source(sid: str, request: Request):
    guard(request)
    body = await body_json(request)
    cfg = config.get()
    for item in cfg["sources"]:
        if item.get("id") == sid:
            for key in ("name", "api", "key", "detail"):
                if key in body:
                    item[key] = subscription.unwrap_proxy(str(body[key]).strip()) \
                        if key == "api" else str(body[key]).strip()[:200]
            if "enabled" in body:
                item["enabled"] = bool(body["enabled"])
            config.save(cfg)
            return ok({"source": item})
    return fail("源不存在", 404)


@router.delete("/sources/{sid}")
async def delete_source(sid: str, request: Request):
    guard(request)
    cfg = config.get()
    left = [s for s in cfg["sources"] if s.get("id") != sid]
    if len(left) == len(cfg["sources"]):
        return fail("源不存在", 404)
    cfg["sources"] = left
    config.save(cfg)
    return ok({"deleted": sid})


@router.post("/sources/batch")
async def batch_sources(request: Request):
    guard(request)
    body = await body_json(request)
    action = body.get("action")
    ids = set(body.get("ids") or [])
    if action not in ("enable", "disable", "delete"):
        return fail("未知批量操作")
    if not ids:
        return fail("请先选择视频源")
    cfg = config.get()
    before = len(cfg["sources"])
    if action == "delete":
        cfg["sources"] = [s for s in cfg["sources"] if s.get("id") not in ids]
        changed = before - len(cfg["sources"])
    else:
        changed = 0
        wanted = action == "enable"
        for item in cfg["sources"]:
            if item.get("id") in ids and bool(item.get("enabled")) != wanted:
                item["enabled"] = wanted
                changed += 1
    config.save(cfg)
    return ok({"changed": changed, "action": action,
               "total": len(cfg["sources"])})


PROBE_BUDGET = 12.0
PROBE_CONCURRENCY = 4
PROBE_SOURCE_TIMEOUT = 3.0


@router.post("/sources/probe")
async def probe_sources(request: Request):
    guard(request)
    body = await body_json(request)
    ids = set(body.get("ids") or [])
    keyword = str(body.get("keyword") or "").strip()[:40]
    if not keyword:
        return fail("请输入检测关键词")
    pool = config.all_sources()
    sources = [s for s in pool if s.get("id") in ids] if ids else pool
    if not sources:
        return ok({"results": [], "total": 0, "done": 0, "timed_out": 0,
                   "budget_ms": int(PROBE_BUDGET * 1000), "elapsed_ms": 0})
    sem = asyncio.Semaphore(PROBE_CONCURRENCY)
    started = time.monotonic()
    deadline = started + PROBE_BUDGET

    async def one(source):
        t0 = time.monotonic()
        async with sem:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"id": source["id"], "ok": False, "count": 0,
                        "timed_out": True}
            try:
                found = await cms.search_source(
                    request.app.state.src, source, keyword,
                    timeout=min(PROBE_SOURCE_TIMEOUT, remaining))
                return {"id": source["id"], "ok": bool(found),
                        "count": len(found),
                        "elapsed_ms": int((time.monotonic() - t0) * 1000)}
            except Exception as exc:  
                return {"id": source["id"], "ok": False, "count": 0,
                        "error": type(exc).__name__}

    tasks = [asyncio.create_task(one(s)) for s in sources]
    done, pending = await asyncio.wait(tasks, timeout=PROBE_BUDGET)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    results = [task.result() for task in done
               if not task.cancelled() and task.exception() is None]
    return ok({"results": results, "total": len(sources),
               "done": len(results), "timed_out": len(pending),
               "budget_ms": int(PROBE_BUDGET * 1000),
               "elapsed_ms": int((time.monotonic() - started) * 1000)})


@router.post("/sources/{sid}/test")
async def test_source(sid: str, request: Request):
    guard(request)
    source = next((s for s in config.all_sources() if s.get("id") == sid), None)
    if not source:
        return fail("源不存在", 404)
    started = time.monotonic()
    try:
        found = await cms.search_source(request.app.state.src, source, "测试", timeout=3.0)
        return ok({"ok": bool(found), "count": len(found),
                   "elapsed_ms": int((time.monotonic() - started) * 1000)})
    except Exception as exc:  
        return ok({"ok": False, "count": 0, "error": type(exc).__name__})


@router.post("/subscriptions/fetch")
async def fetch_subscription(request: Request):
    guard(request)
    body = await body_json(request)
    try:
        result = await fetch_and_apply(url=body.get("url"), text=body.get("text"))
    except Exception as exc:  
        return fail(f"订阅拉取失败：{exc}", 502)
    runtime.log(f"subscription fetched: {result['enabled']} sources from {result['url'] or 'text'}")
    return ok(result)


@router.patch("/subscriptions/settings")
async def subscription_settings(request: Request):
    guard(request)
    body = await body_json(request)
    cfg = config.get()
    for key in ("subscription_url", "auto_update", "update_interval_hours"):
        if key in body:
            cfg[key] = body[key]
    config.save(cfg)
    return ok({k: cfg[k] for k in ("subscription_url", "auto_update",
                                   "update_interval_hours", "last_update")})


@router.post("/subscriptions/content")
async def save_subscription_content(request: Request):
    guard(request)
    body = await body_json(request)
    text = str(body.get("text") or "").strip()
    if not text:
        return fail("配置内容为空")
    try:
        result = await fetch_and_apply(text=text)
    except Exception as exc:  
        return fail(f"配置解析失败：{exc}", 400)
    runtime.log(f"subscription content applied: {result['enabled']} sources")
    return ok(result)


@router.post("/subscriptions/preview")
async def preview_subscription(request: Request):
    guard(request)
    body = await body_json(request)
    text = str(body.get("text") or "").strip()
    url = str(body.get("url") or "").strip()
    try:
        if not text and url:
            text = await subscription.fetch(url)
        obj = subscription.decode_payload(text)
        items, report = subscription.normalize(obj)
    except Exception as exc:  
        return fail(f"订阅解析失败：{exc}", 502)
    return ok({"candidates": items, "report": report, "url": url})


@router.post("/subscriptions/apply")
async def apply_subscription(request: Request):
    guard(request)
    body = await body_json(request)
    candidates = body.get("candidates") or []
    enabled_ids = set(body.get("enabled_ids") or [])
    url = str(body.get("url") or "").strip()
    if not isinstance(candidates, list):
        return fail("候选列表无效")
    clean, _ = subscription.normalize({"sources": candidates}, origin="subscription")
    cfg = config.get()
    merged, stats = subscription.merge(clean, cfg["sources"])
    limit = int(cfg.get("enabled_limit", 12))
    picked = 0
    for item in merged:
        if item.get("id") in enabled_ids and picked < limit:
            item["enabled"] = True
            picked += 1
    cfg["sources"] = merged
    if url:
        subs = [s for s in cfg["subscriptions"] if s.get("url") != url]
        subs.append({"url": url, "enabled": True, "last_sync": int(time.time())})
        cfg["subscriptions"] = subs
    config.save(cfg)
    return ok({**stats, "enabled": picked, "total": len(merged)})


@router.delete("/cache")
async def clear_cache(request: Request):
    guard(request)
    return ok({"cleared": cms.clear_cache()})


def vlib_status() -> dict:
    return {
        "enabled": vlib.enabled(),
        "charts_enabled": vlib.charts_enabled(),
        "catalog_enabled": vlib.catalog_enabled(),
        "charts": charts.chart_cache.status(),
        "catalog": catalog.catalog.status(),
        "bindings": vlib.bind_count(),
    }


@router.get("/vlib/status")
async def vlib_status_route(request: Request):
    guard(request)
    return ok(vlib_status())


@router.post("/vlib/refresh")
async def vlib_refresh(request: Request):
    """立即刷新榜单与分类路由；失败保留旧缓存，结果如实返回。"""
    guard(request)
    body = await body_json(request)
    what = str(body.get("what") or "all")
    client = request.app.state.src
    if what in ("all", "charts"):
        await charts.chart_cache.refresh(client)
    if what in ("all", "catalog"):
        await catalog.catalog.refresh_routes(client, config.enabled_sources())
    return ok(vlib_status())


@router.delete("/vlib/cache")
async def vlib_clear(request: Request):
    guard(request)
    charts.chart_cache.reset()
    catalog.catalog.reset()
    return ok(vlib_status())


@router.get("/logs")
async def logs(request: Request):
    return ok({"logs": runtime.recent(int(request.query_params.get("limit", 100)))})


@router.get("/adfilter/preview")
async def adfilter_preview(request: Request):
    guard(request)
    return ok({"previews": hls.recent_ad_previews(3)})


@router.get("/export")
async def export_config():
    return ok({"config": _public_config()})


@router.post("/import")
async def import_config(request: Request):
    guard(request)
    body = await body_json(request)
    incoming = body.get("config")
    if not isinstance(incoming, dict):
        return fail("配置格式无效")
    allowed = {k: incoming[k] for k in config.DEFAULTS
               if k in incoming and k != "admin_token"}
    cfg = config.get()
    cfg.update(allowed)
    config.save(cfg)
    return ok({"config": {k: v for k, v in cfg.items() if k != "admin_token"}})
