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
    "enabled_limit": 200,
    "search_timeout": 2.5,
    "search_concurrency": 5,
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
}

_NUMERIC = {"enabled_limit": (1, 200), "search_timeout": (0.5, 10.0),
            "search_concurrency": (1, 16), "play_timeout": (2.0, 30.0),
            "update_interval_hours": (1, 168), "adfilter_head_skip": (0.0, 120.0)}
_INTEGERS = ("enabled_limit", "search_concurrency", "update_interval_hours")
_LOCK = threading.RLock()
_cache: dict | None = None
_mtime = 0.0


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


def all_sources() -> list[dict]:
    return [dict(s) for s in get().get("sources", []) if s.get("api")]


def token() -> str:
    return str(get().get("admin_token") or "")
