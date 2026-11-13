#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import acl
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
        return version_file.read_text(encoding="utf-8").strip() or "0.0.1.18"
    except OSError:
        return "0.0.1.18"


def _public_config() -> dict:
    cfg = config.get()
    cfg.pop("admin_token", None)
    # 旧版本留下的设备绑定对本机之外无意义，导出时一并剥掉。
    cfg.pop("acl_devices", None)
    return cfg


def _acl_summary() -> dict:
    ident = acl.current()
    grant = (config.get().get("acl_grants") or {}).get(ident.uid) or {}
    return {
        "enabled": acl.enabled(),
        "known": ident.known,
        "uid": ident.uid,
        "name": ident.name,
        "via": ident.via,
        "reason": ident.reason,
        "libs": len(grant.get("libs") or []),
        "sources": len(grant.get("sources") or []),
        "sources_total": len(config.enabled_sources()),
    }


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
        "acl": _acl_summary(),
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
    # 源被删除后配置里已没有这个 id，播放链无法再判断它失效，只能在这里清。
    try:
        vlib.purge_provider(request.app.state.store, sid)
    except Exception as exc:  # noqa: BLE001 - 清理失败不影响删除本身
        runtime.log(f"purge provider failed: {type(exc).__name__}")
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


# --------------------------------------------------------------- 账号权限

def _acl_payload() -> dict:
    """管理页需要的完整视图：状态 + 可选库 + 可选源。"""
    status = acl.status()
    status["libs"] = vlib.library_keys()
    status["sources"] = [
        {"id": str(s.get("id") or ""), "name": str(s.get("name") or ""),
         "enabled": bool(s.get("enabled"))}
        for s in config.all_sources()
    ]
    return status


@router.get("/acl/status")
async def acl_status(request: Request):
    guard(request)
    return ok(_acl_payload())


@router.get("/acl/probe")
async def acl_probe(request: Request):
    """自检：当前请求带了什么凭证、被识别成谁、官方库发现了哪些表。"""
    guard(request)
    return ok(acl.probe(request))


@router.patch("/acl/config")
async def acl_config(request: Request):
    guard(request)
    body = await body_json(request)
    cfg = config.get()
    # 兼容两种字段名：管理页发 enabled，配置键叫 acl_enabled。只认其中一种
    # 会导致「勾了总开关又弹回未勾选」，而且因为返回 200 连报错都没有。
    if "enabled" in body:
        wanted = bool(body.get("enabled"))
    elif "acl_enabled" in body:
        wanted = bool(body.get("acl_enabled"))
    else:
        wanted = None
    turning_on = bool(wanted) and not cfg.get("acl_enabled")
    if turning_on:
        # 只有「默认全禁 + 一条授权都没有」才会出现瞬间全员无内容、
        # 连管理员也救不回来的死锁；默认放行时开启是安全的，不拦。
        grants = cfg.get("acl_grants") or {}
        defaults_deny = (not cfg.get("acl_default_visible", True)
                         and not cfg.get("acl_default_online", True))
        if defaults_deny and not any(
                (g or {}).get("libs") or (g or {}).get("sources")
                for g in grants.values()):
            return fail("默认策略是全禁，请先给至少一个账号勾选媒体库或片源，"
                        "或把默认策略改成放行，再开启账号权限")
    if wanted is not None:
        cfg["acl_enabled"] = wanted
    for key in ("acl_subjects", "acl_grants",
                "acl_default_visible", "acl_default_online"):
        if key in body:
            cfg[key] = body[key]
    config.save(cfg)
    acl.invalidate()
    runtime.log(f"acl: enabled={cfg.get('acl_enabled')} "
                f"subjects={len(cfg.get('acl_subjects') or [])} "
                f"grants={len(cfg.get('acl_grants') or [])}")
    return ok(_acl_payload())


@router.post("/acl/grant")
async def acl_grant(request: Request):
    guard(request)
    body = await body_json(request)
    uid = str(body.get("uid") or "").strip()[:64]
    if not uid:
        return fail("请先选择账号")
    cfg = config.get()
    grants = dict(cfg.get("acl_grants") or {})
    entry: dict = {}
    if "libs" in body:
        entry["libs"] = body.get("libs") or []
    if "sources" in body:
        entry["sources"] = body.get("sources") or []
    if "online" in body:
        entry["online"] = bool(body.get("online"))
    grants[uid] = entry
    cfg["acl_grants"] = grants
    if body.get("name"):
        subjects = [dict(s) for s in (cfg.get("acl_subjects") or [])]
        if not any(str(s.get("id")) == uid for s in subjects):
            subjects.append({"id": uid, "name": str(body["name"])[:60],
                             "source": "manual"})
            cfg["acl_subjects"] = subjects
    config.save(cfg)
    acl.invalidate(uid)
    return ok(_acl_payload())


@router.delete("/acl/grant/{uid}")
async def acl_revoke(uid: str, request: Request):
    guard(request)
    cfg = config.get()
    grants = dict(cfg.get("acl_grants") or {})
    grants.pop(uid, None)
    cfg["acl_grants"] = grants
    config.save(cfg)
    acl.invalidate(uid)
    return ok(_acl_payload())


@router.post("/acl/subjects")
async def acl_add_subject(request: Request):
    guard(request)
    body = await body_json(request)
    uid = str(body.get("id") or "").strip()[:64]
    name = str(body.get("name") or uid).strip()[:60] or uid
    if not uid:
        return fail("账号标识不能为空（建议填 fnOS 的 user_id）")
    cfg = config.get()
    subjects = [dict(s) for s in (cfg.get("acl_subjects") or [])]
    if not any(str(s.get("id")) == uid for s in subjects):
        subjects.append({"id": uid, "name": name, "source": "manual"})
    cfg["acl_subjects"] = subjects
    config.save(cfg)
    return ok(_acl_payload())


@router.delete("/acl/subjects/{uid}")
async def acl_del_subject(uid: str, request: Request):
    """从清单移除账号，并连带清掉它的授权。

    只删 ``acl_subjects`` 是不够的：``acl.subjects()`` 会把「仍有授权」的
    uid 重新补回清单，表现就是「删了还在」。显式删除视为彻底移除；授权需要
    保留时请用「恢复默认策略」而不是删账号。
    """
    guard(request)
    cfg = config.get()
    cfg["acl_subjects"] = [s for s in (cfg.get("acl_subjects") or [])
                           if str(s.get("id")) != uid]
    grants = dict(cfg.get("acl_grants") or {})
    dropped = grants.pop(uid, None) is not None
    cfg["acl_grants"] = grants
    config.save(cfg)
    acl.invalidate(uid)
    runtime.log(f"acl: del subject {uid} grants={'yes' if dropped else 'no'}")
    return ok(_acl_payload())


@router.patch("/ui/theme")
async def set_ui_theme(request: Request):
    """记住管理页主题。

    浏览器 localStorage 在飞牛 App 内嵌页/隐私模式下可能不可用，导致
    「切了主题、下次打开又变回去」。主题存服务端后与设备无关。
    """
    guard(request)
    body = await body_json(request)
    theme = str(body.get("theme") or "").strip()
    if theme not in ("light", "dark", "system"):
        return fail("主题只能是 light / dark / system")
    cfg = config.get()
    cfg["ui_theme"] = theme
    config.save(cfg)
    return ok({"theme": theme})


@router.post("/acl/discover")
async def acl_discover(request: Request):
    guard(request)
    report = acl.discover(force=True)
    return ok(report)


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
