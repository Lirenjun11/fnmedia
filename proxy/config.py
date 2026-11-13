#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import os
import secrets
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.environ.get("FNOS_MEDIA_STATE") or (ROOT / "state"))
CONFIG_FILE = STATE_DIR / "config.json"

DEFAULTS = {
    "sources": [],
    "subscriptions": [],
    "subscription_url": "",
    "auto_update": False,
    "update_interval_hours": 24,
    "config_text": "",
    "last_update": 0,
    # 管理页外观主题：light / dark / system（存服务端，换浏览器/清缓存也记得住）
    "ui_theme": "system",
    "enabled_limit": 200,
    "search_timeout": 2.5,
    "search_concurrency": 5,
    "search_debounce_ms": 200,
    "play_timeout": 8.0,
    "online_first": True,
    "merge_sources": True,
    "borrow_official": True,
    "auto_best_source": True,
    "allow_intranet": True,
    "douban_enabled": False,
    "playstate": {},
    "update_repo": "Lirenjun11/fnmedia",
    "admin_token": "",
    "adfilter_enabled": False,
    "adfilter_markers": True,
    "adfilter_keywords": [],
    "adfilter_head_skip": 0.0,
    "adfilter_head_guard": True,
    "adfilter_breaks": True,
    "adfilter_strong": True,
    "vlib_enabled": True,
    "vlib_charts": True,
    "vlib_catalog": True,
    "vlib_refresh_hours": 6,
    # ---- 虚拟库片源映射（空配置 = 旧行为，零回归）----
    # vlib_sources:    选中进虚拟库的源 id；空 = 全部启用源（沿用旧 4+4 库）
    # vlib_categories: 要显示的分类；空 = movie/tv/anime/variety 全开
    # vlib_split:      category = 4 张分类库（内容限选中源）
    #                  source_category = 每个「源 × 分类」一张库卡片
    #                  仅在 vlib_sources 非空时生效，空配置仍是旧 4+4 库
    "vlib_sources": [],
    "vlib_categories": [],
    "vlib_split": "source_category",
    # ---- 账号权限（默认关闭；关闭时全链路短路，行为与旧版一致）----
    # acl_subjects: [{id,name,source}] 授权主体（fnOS 账号）
    # acl_grants:   {uid: {libs: ["chart:movie"], sources: [sid], online: bool}}
    #   - 键不存在 = 该账号未单独配置，按下面的默认策略处理
    #   - libs 存在且为空 = 一个虚拟库都看不到
    #   - sources 存在且为空 = 一条在线片源都不能用
    "acl_enabled": False,
    "acl_subjects": [],
    "acl_grants": {},
    # 默认策略：未单独配置的账号怎么算。默认放行，和参考项目一致 —— 开启
    # 账号权限不会让任何账号瞬间失去内容，要限制谁再去单独取消勾选。
    "acl_default_visible": True,
    "acl_default_online": True,
}

_NUMERIC = {"enabled_limit": (1, 200), "search_timeout": (0.5, 10.0),
            "search_concurrency": (1, 16), "play_timeout": (2.0, 30.0),
            "update_interval_hours": (1, 168), "adfilter_head_skip": (0.0, 120.0),
            "search_debounce_ms": (0, 1000), "vlib_refresh_hours": (1, 24)}
_INTEGERS = ("enabled_limit", "search_concurrency", "update_interval_hours",
             "search_debounce_ms", "vlib_refresh_hours")
_LOCK = threading.RLock()
_cache: dict | None = None
_mtime = 0.0


def _clean_subjects(raw) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    if not isinstance(raw, list):
        return out
    for row in raw[:200]:
        if not isinstance(row, dict):
            continue
        uid = str(row.get("id") or "").strip()[:64]
        if not uid or uid in seen:
            continue
        seen.add(uid)
        out.append({
            "id": uid,
            "name": str(row.get("name") or uid).strip()[:60] or uid,
            "source": "auto" if row.get("source") == "auto" else "manual",
        })
    return out


def _clean_grants(raw) -> dict:
    """主体授权：{uid: {libs?: [key], sources?: [sid], online?: bool}}。

    **保留「键是否存在」的语义**：没有 libs 键 = 未单独配置（走默认策略），
    libs 为空数组 = 明确不允许任何虚拟库。旧实现无条件补成空数组，会把
    「没配」误判成「全禁」，一开启就让人看不到东西。
    """
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for uid, value in list(raw.items())[:200]:
        uid = str(uid or "").strip()[:64]
        if not uid or not isinstance(value, dict):
            continue
        item: dict = {}
        if "online" in value:
            item["online"] = bool(value.get("online"))
        libs = value.get("libs")
        if isinstance(libs, list):
            item["libs"] = sorted({str(x).strip()[:40] for x in libs[:64]
                                   if str(x).strip()})
        sources = value.get("sources")
        if isinstance(sources, list):
            item["sources"] = sorted({str(x).strip()[:40] for x in sources[:400]
                                      if str(x).strip()})
        if item:
            out[uid] = item
    return out


def _ensure_token(cfg: dict) -> bool:
    if cfg.get("admin_token"):
        return False
    cfg["admin_token"] = secrets.token_urlsafe(24)
    return True


def _read() -> dict:
    global _cache, _mtime
    cfg = copy.deepcopy(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                for key in DEFAULTS:
                    if key in saved:
                        cfg[key] = saved[key]
        except (OSError, ValueError):
            pass
    changed = _ensure_token(cfg)
    _cache = cfg
    try:
        _mtime = CONFIG_FILE.stat().st_mtime
    except OSError:
        _mtime = 0.0
    if changed:
        _write(cfg)
    return cfg


def _write(cfg: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)
    global _cache, _mtime
    _cache = copy.deepcopy(cfg)
    try:
        _mtime = CONFIG_FILE.stat().st_mtime
    except OSError:
        _mtime = 0.0


def get() -> dict:
    with _LOCK:
        try:
            m = CONFIG_FILE.stat().st_mtime
        except OSError:
            m = 0.0
        if _cache is None or m != _mtime:
            return copy.deepcopy(_read())
        return copy.deepcopy(_cache)


def save(cfg: dict) -> dict:
    clean = {k: copy.deepcopy(cfg.get(k, DEFAULTS[k])) for k in DEFAULTS}
    for key, (lo, hi) in _NUMERIC.items():
        try:
            value = float(clean[key])
        except (TypeError, ValueError):
            value = DEFAULTS[key]
        clean[key] = int(max(lo, min(hi, value))) if key in _INTEGERS \
            else round(max(lo, min(hi, value)), 2)
    clean["online_first"] = bool(clean["online_first"])
    clean["merge_sources"] = bool(clean["merge_sources"])
    clean["allow_intranet"] = bool(clean["allow_intranet"])
    clean["adfilter_enabled"] = bool(clean["adfilter_enabled"])
    clean["adfilter_markers"] = bool(clean["adfilter_markers"])
    clean["adfilter_head_guard"] = bool(clean["adfilter_head_guard"])
    clean["adfilter_breaks"] = bool(clean["adfilter_breaks"])
    clean["adfilter_strong"] = bool(clean["adfilter_strong"])
    clean["vlib_enabled"] = bool(clean["vlib_enabled"])
    clean["vlib_charts"] = bool(clean["vlib_charts"])
    clean["vlib_catalog"] = bool(clean["vlib_catalog"])
    clean["acl_enabled"] = bool(clean["acl_enabled"])
    clean["acl_subjects"] = _clean_subjects(clean["acl_subjects"])
    clean["acl_grants"] = _clean_grants(clean["acl_grants"])
    clean["acl_default_visible"] = bool(clean["acl_default_visible"])
    clean["acl_default_online"] = bool(clean["acl_default_online"])
    if clean.get("ui_theme") not in ("light", "dark", "system"):
        clean["ui_theme"] = DEFAULTS["ui_theme"]
    raw_keywords = clean["adfilter_keywords"]
    if not isinstance(raw_keywords, list):
        raw_keywords = []
    clean["adfilter_keywords"] = [
        str(word).strip().lower()[:64] for word in raw_keywords[:64]
        if str(word).strip()
    ][:32]
    clean["sources"] = [s for s in clean["sources"] if isinstance(s, dict) and s.get("api")]
    clean["subscriptions"] = [s for s in clean["subscriptions"]
                              if isinstance(s, dict) and s.get("url")]
    # 虚拟库映射：源 id 必须真实存在。订阅全量拉取会整体替换 sources[]，
    # 所以这份映射只能独立存键，不能挂在源对象上。
    known_ids = {str(s.get("id") or "") for s in clean["sources"]
                 if s.get("id")}
    raw_picked = clean["vlib_sources"]
    if not isinstance(raw_picked, list):
        raw_picked = []
    clean["vlib_sources"] = sorted({
        str(x).strip()[:40] for x in raw_picked[:64]
        if str(x).strip() and str(x).strip() in known_ids})
    raw_cats = clean["vlib_categories"]
    if not isinstance(raw_cats, list):
        raw_cats = []
    wanted_cats = {str(x).strip() for x in raw_cats}
    clean["vlib_categories"] = [
        c for c in ("movie", "tv", "anime", "variety") if c in wanted_cats]
    clean["vlib_split"] = ("source_category"
                           if clean["vlib_split"] == "source_category"
                           else "category")
    with _LOCK:
        _write(clean)
    return copy.deepcopy(clean)


def patch(partial: dict) -> dict:
    cfg = get()
    for key in DEFAULTS:
        if key in partial and key not in ("sources", "subscriptions", "admin_token"):
            cfg[key] = partial[key]
    return save(cfg)


def enabled_sources() -> list[dict]:
    cfg = get()
    limit = int(cfg.get("enabled_limit", 12))
    return [dict(s) for s in cfg.get("sources", [])
            if s.get("enabled") and s.get("api")][:limit]


_ids_cache: tuple | None = None


def _source_sets() -> tuple[frozenset[str], frozenset[str]]:
    """(配置里出现过的源 id, 当前启用的源 id)，按源配置签名缓存。"""
    global _ids_cache
    rows = get().get("sources", [])
    sig = tuple(sorted((str(s.get("id") or ""), bool(s.get("enabled")),
                        str(s.get("api") or "")) for s in rows))
    if _ids_cache is not None and _ids_cache[0] == sig:
        return _ids_cache[1], _ids_cache[2]
    known = frozenset(str(s.get("id") or "") for s in rows if s.get("id"))
    enabled = frozenset(str(s.get("id") or "") for s in rows
                        if s.get("id") and s.get("enabled") and s.get("api"))
    _ids_cache = (sig, known, enabled)
    return known, enabled


def enabled_source_ids() -> frozenset[str]:
    """当前启用源 id 集合（不受 enabled_limit 截断）。

    播放/详情要用它判断一条已有数据是否还允许使用：源被禁用后，之前
    搜到的线路必须立刻失效，而不是继续从禁用源取流。
    """
    return _source_sets()[1]


def source_live(provider: str) -> bool:
    """provider 是否仍可用。

    只有「源仍在配置里、但已被禁用」才算失效；配置里压根没有这个 id 的
    数据（历史遗留、外部构造的 brief）保持原行为，不做误杀。
    """
    pid = str(provider or "")
    if not pid:
        return True
    known, enabled = _source_sets()
    if pid not in known:
        return True
    return pid in enabled


def all_sources() -> list[dict]:
    return [dict(s) for s in get().get("sources", []) if s.get("api")]


def token() -> str:
    return str(get().get("admin_token") or "")
