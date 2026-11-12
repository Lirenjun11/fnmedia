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


def set_watched(guid: str, episode: str = "") -> None:
    load()
    with _lock:
        prev = _watched_entry(guid)
        _watched[guid] = {"at": int(time.time()),
                          "episode": episode or prev.get("episode") or ""}
    persist(force=True)


def _watched_entry(guid: str) -> dict:
    """已看记录兼容旧格式（纯时间戳）与新格式（含最后观看集）。"""
    raw = _watched.get(guid)
    if isinstance(raw, dict):
        return raw
    if raw:
        return {"at": int(raw), "episode": ""}
    return {}


def remove_watched(guid: str) -> None:
    load()
    with _lock:
        _watched.pop(guid, None)
    persist(force=True)


def record(body: dict, brief, duration_hint: float = 0.0) -> None:
    """记录播放进度。

    同一部剧的进度按“最后观看的那一集”保存：换集时不能用旧集的位置取
    max，否则从第 3 集 100 秒切回第 2 集 300 秒会把续播点错算成 300 秒。
    """
    load()
    guid = str(body.get("item_guid") or body.get("guid") or "")
    if not guid:
        return
    episode = str(body.get("episode_guid") or "")
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
        prev = dict(_history.get(guid) or {})
        if duration > 0 and ts >= duration * 0.95:
            _history.pop(guid, None)
            _watched[guid] = {"at": int(time.time()), "episode": episode}
            finished = True
        else:
            finished = False
            prev_episode = str(prev.get("episode_guid") or "")
            crossed = bool(episode and prev_episode and episode != prev_episode)
            pos = ts if crossed else max(ts, float(prev.get("ts") or 0))
            prev.update({
                "item_guid": guid,
                "episode_guid": episode or prev_episode,
                "title": (brief or {}).get("title") or prev.get("title") or "",
                "poster": (brief or {}).get("poster") or prev.get("poster") or "",
                "type": "TV" if (brief or {}).get("is_tv") else "Movie",
                "ts": pos,
                "duration": duration or float(prev.get("duration") or 0),
                "media_guid": body.get("media_guid") or prev.get("media_guid") or "",
                "video_guid": body.get("video_guid") or prev.get("video_guid") or "",
                "updated_at": int(time.time()),
            })
            _history[guid] = prev
            _trim()
    persist(force=finished)


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
    """已看标记时间（unix 秒），不是播放进度。进度请用 ts_for。"""
    load()
    with _lock:
        return int(_watched_entry(guid).get("at") or 0)


def ts_for(guid: str) -> int:
    """续播位置（秒）。官方客户端的 play/info 就是拿这个值 seek。"""
    load()
    with _lock:
        return int((_history.get(guid) or {}).get("ts") or 0)


def episode_for(guid: str) -> str:
    load()
    with _lock:
        return str((_history.get(guid) or {}).get("episode_guid") or "")


def episode_ts(guid: str, episode: str) -> int:
    """某集的进度，只在该集是最后观看的一集时返回。"""
    load()
    with _lock:
        rec = _history.get(guid) or {}
        if str(rec.get("episode_guid") or "") != (episode or ""):
            return 0
        return int(rec.get("ts") or 0)


def media_for(guid: str) -> str:
    load()
    with _lock:
        return str((_history.get(guid) or {}).get("media_guid") or "")


def duration_for(guid: str) -> int:
    load()
    with _lock:
        return int((_history.get(guid) or {}).get("duration") or 0)


def apply(row: dict, guid: str, rec_guid: str | None = None) -> dict:
    """把本地状态回填到官方 item 结构。

    `watched_ts` 在官方接口里是「上次播放到第几秒」（客户端拿它 seek），
    不是「标记已看的时间」。已看时间只放进 watched_at，避免客户端把
    unix 时间戳当成播放位置跳到片尾。
    """
    key = rec_guid or guid
    load()
    with _lock:
        fav = guid in _favorites or key in _favorites
        stamp = int(_watched_entry(guid).get("at")
                    or _watched_entry(key).get("at") or 0)
        rec = dict(_history.get(key) or {})
    pos = int(rec.get("ts") or 0)
    duration = int(rec.get("duration") or 0)
    row["is_favorite"] = 1 if fav else 0
    row["watched"] = 1 if stamp else 0
    row["is_watched"] = 1 if stamp else 0
    row["watched_at"] = stamp
    row["watched_ts"] = pos
    row["ts"] = pos
    if duration > 0:
        row["duration"] = duration
    return row
