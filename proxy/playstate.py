"""Playback state: favorites, watched marks and progress, stored in config.json."""
from __future__ import annotations

import threading
import time

import config

_MAX_FAVORITES = 500
_MAX_HISTORY = 100
_WRITE_THROTTLE = 10.0
_lock = threading.RLock()
_favorites = {}
_watched = {}
_history = {}
_last_write = 0.0
_loaded = False


def load() -> None:
    global _loaded, _favorites, _watched, _history
    with _lock:
        if _loaded:
            return
        state = config.get().get("playstate") or {}
        _favorites = dict(state.get("favorites") or {})
        _watched = dict(state.get("watched") or {})
        _history = dict(state.get("history") or {})
        _loaded = True


def snapshot() -> dict:
    with _lock:
        return {"favorites": dict(_favorites), "watched": dict(_watched),
                "history": dict(_history)}


def persist(force: bool = False) -> None:
    global _last_write
    now = time.monotonic()
    with _lock:
        if not force and now - _last_write < _WRITE_THROTTLE:
            return
        cfg = config.get()
        cfg["playstate"] = snapshot()
        config.save(cfg)
        _last_write = now


def _trim() -> None:
    if len(_favorites) > _MAX_FAVORITES:
        oldest = sorted(_favorites, key=lambda g: _favorites[g].get("ts", 0))[
            :len(_favorites) - _MAX_FAVORITES]
        for guid in oldest:
            _favorites.pop(guid, None)
    if len(_history) > _MAX_HISTORY:
        oldest = sorted(_history,
                        key=lambda g: _history[g].get("updated_at", 0))[
            :len(_history) - _MAX_HISTORY]
        for guid in oldest:
            _history.pop(guid, None)


def add_favorite(guid: str, brief) -> None:
    load()
    with _lock:
        _favorites[guid] = {
            "title": (brief or {}).get("title") or "",
            "poster": (brief or {}).get("poster") or "",
            "type": "TV" if (brief or {}).get("is_tv") else "Movie",
            "ts": int(time.time()),
        }
        _trim()
    persist(force=True)


def remove_favorite(guid: str) -> None:
    load()
    with _lock:
        _favorites.pop(guid, None)
    persist(force=True)


def favorite_guids() -> list:
    load()
    with _lock:
        return list(_favorites)


def set_watched(guid: str) -> None:
    load()
    with _lock:
        _watched[guid] = int(time.time())
    persist(force=True)


def remove_watched(guid: str) -> None:
    load()
    with _lock:
        _watched.pop(guid, None)
    persist(force=True)


def record(body: dict, brief, duration_hint: float = 0.0) -> None:
    load()
    guid = str(body.get("item_guid") or body.get("guid") or "")
    if not guid:
        return
    try:
        ts = float(body.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    try:
        duration = float(body.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        duration = float(duration_hint or 0)
    with _lock:
        if duration > 0 and ts >= duration * 0.95:
            _history.pop(guid, None)
            _watched[guid] = int(time.time())
        else:
            rec = dict(_history.get(guid) or {})
            rec.update({
                "item_guid": guid,
                "title": (brief or {}).get("title") or rec.get("title") or "",
                "poster": (brief or {}).get("poster") or rec.get("poster") or "",
                "type": "TV" if (brief or {}).get("is_tv") else "Movie",
                "ts": max(ts, float(rec.get("ts") or 0)),
                "duration": duration,
                "media_guid": body.get("media_guid") or "",
                "video_guid": body.get("video_guid") or "",
                "updated_at": int(time.time()),
            })
            _history[guid] = rec
            _trim()
    persist()


def delete_records(ids: list) -> None:
    load()
    with _lock:
        for guid in ids:
            _history.pop(guid, None)
    persist(force=True)


def history_records() -> list:
    load()
    with _lock:
        rows = [(guid, dict(rec)) for guid, rec in _history.items()]
    rows.sort(key=lambda pair: pair[1].get("updated_at", 0), reverse=True)
    return rows


def is_favorite(guid: str) -> bool:
    load()
    with _lock:
        return guid in _favorites


def watched_ts(guid: str) -> int:
    load()
    with _lock:
        return int(_watched.get(guid) or 0)


def ts_for(guid: str) -> int:
    load()
    with _lock:
        return int((_history.get(guid) or {}).get("ts") or 0)


def duration_for(guid: str) -> int:
    load()
    with _lock:
        return int((_history.get(guid) or {}).get("duration") or 0)


def apply(row: dict, guid: str) -> dict:
    load()
    with _lock:
        fav = guid in _favorites
        stamp = int(_watched.get(guid) or 0)
        rec = dict(_history.get(guid) or {})
    row["is_favorite"] = 1 if fav else 0
    row["watched"] = 1 if stamp else 0
    row["is_watched"] = 1 if stamp else 0
    row["watched_ts"] = stamp
    row["ts"] = int(rec.get("ts") or 0)
    row["duration"] = int(rec.get("duration") or 0)
    return row
