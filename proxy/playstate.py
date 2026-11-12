"""Playback state: favorites, watched marks and progress, stored in config.json.

多账号隔离（对齐参考项目 v0.2.48 的做法）：

- 状态按飞牛账号分区：``_users[uid] = {favorites, watched, history}``；
- 未识别身份（ACL 关闭 / 解析失败）落到全局分区 ``""``，行为与旧版一致；
- 升级迁移：旧的全局记录只由**管理员**在首次访问时继承一次（管理员通常是原先
  的主要使用者），子账号从空白开始，不再互相串进度；
- 分区裁剪：每个账号各自最多保留 ``_MAX_FAVORITES`` / ``_MAX_HISTORY`` 条。
"""
from __future__ import annotations

import threading
import time

import acl
import config

_MAX_FAVORITES = 500
_MAX_HISTORY = 100
_MAX_USERS = 50
_WRITE_THROTTLE = 10.0
_lock = threading.RLock()
# {uid: {"favorites": {...}, "watched": {...}, "history": {...}}}
_users: dict[str, dict[str, dict]] = {}
# 已从全局分区继承过旧记录的账号（持久化，避免重启后重复继承）
_adopted: set[str] = set()
_last_write = 0.0
_loaded = False


def _empty() -> dict:
    return {"favorites": {}, "watched": {}, "history": {}}


def _migrate(state: dict) -> None:
    """旧格式 ``{favorites/watched/history}`` → 全局分区 ``users[""]``。"""
    global _users, _adopted
    users = state.get("users")
    if isinstance(users, dict):
        _users = {}
        for uid, data in users.items():
            if not isinstance(data, dict):
                continue
            _users[str(uid)] = {
                "favorites": dict(data.get("favorites") or {}),
                "watched": dict(data.get("watched") or {}),
                "history": dict(data.get("history") or {}),
            }
        _adopted = {str(x) for x in (state.get("adopted") or [])}
        return
    _users = {"": {
        "favorites": dict(state.get("favorites") or {}),
        "watched": dict(state.get("watched") or {}),
        "history": dict(state.get("history") or {}),
    }}
    _adopted = set()


def load() -> None:
    global _loaded
    with _lock:
        if _loaded:
            return
        _migrate(config.get().get("playstate") or {})
        _loaded = True


def reset() -> None:
    """测试用：丢弃内存态，下次访问重新从配置加载。"""
    global _loaded, _users, _adopted
    with _lock:
        _users = {}
        _adopted = set()
        _loaded = False


def current_uid() -> str:
    """当前请求的账号；未识别返回 ""（= 全局分区，与旧版行为一致）。"""
    try:
        return acl.current().uid or ""
    except Exception:  # noqa: BLE001 - 任何异常都退回全局分区
        return ""


def _bucket(uid: str | None = None) -> dict:
    key = current_uid() if uid is None else str(uid or "")
    bucket = _users.get(key)
    if bucket is None:
        bucket = _empty()
        _users[key] = bucket
    if key:
        _adopt_if_needed(key, bucket)
    return bucket


def _adopt_if_needed(uid: str, bucket: dict) -> None:
    """升级迁移：旧版历史全家共享，只让管理员首次访问时继承一次。"""
    if not uid or uid in _adopted:
        return
    if any(bucket[k] for k in ("favorites", "watched", "history")):
        _adopted.add(uid)
        return
    if not acl.is_admin(uid):
        return
    legacy = _users.get("")
    if not legacy or not any(legacy[k] for k in ("favorites", "watched", "history")):
        _adopted.add(uid)
        return
    for name in ("favorites", "watched", "history"):
        bucket[name].update({k: dict(v) if isinstance(v, dict) else v
                             for k, v in legacy[name].items()})
    _adopted.add(uid)


def snapshot() -> dict:
    with _lock:
        return {
            "users": {
                uid: {name: dict(data.get(name) or {})
                      for name in ("favorites", "watched", "history")}
                for uid, data in _users.items()
                if any(data.get(n) for n in ("favorites", "watched", "history"))
            },
            "adopted": sorted(_adopted),
        }


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


def _trim_bucket(bucket: dict) -> None:
    fav = bucket["favorites"]
    if len(fav) > _MAX_FAVORITES:
        oldest = sorted(fav, key=lambda g: fav[g].get("ts", 0))[
            :len(fav) - _MAX_FAVORITES]
        for guid in oldest:
            fav.pop(guid, None)
    hist = bucket["history"]
    if len(hist) > _MAX_HISTORY:
        oldest = sorted(hist, key=lambda g: hist[g].get("updated_at", 0))[
            :len(hist) - _MAX_HISTORY]
        for guid in oldest:
            hist.pop(guid, None)


def _trim() -> None:
    for bucket in _users.values():
        _trim_bucket(bucket)
    if len(_users) > _MAX_USERS:
        # 空分区先丢，再按最后活跃时间淘汰，避免长期累积。
        for uid in [u for u, b in _users.items()
                    if u and not any(b[n] for n in ("favorites", "watched",
                                                    "history"))]:
            _users.pop(uid, None)
        if len(_users) > _MAX_USERS:
            ranked = sorted(
                _users,
                key=lambda u: max(
                    [v.get("updated_at", 0) for v in _users[u]["history"].values()]
                    + [v.get("ts", 0) for v in _users[u]["favorites"].values()]
                    + [0]),
                reverse=True)
            for uid in ranked[_MAX_USERS:]:
                _users.pop(uid, None)


def add_favorite(guid: str, brief) -> None:
    load()
    with _lock:
        bucket = _bucket()
        bucket["favorites"][guid] = {
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
        _bucket()["favorites"].pop(guid, None)
    persist(force=True)


def favorite_guids() -> list:
    load()
    with _lock:
        return list(_bucket()["favorites"])


def set_watched(guid: str, episode: str = "") -> None:
    load()
    with _lock:
        bucket = _bucket()
        prev = _watched_entry(guid, bucket)
        bucket["watched"][guid] = {
            "at": int(time.time()),
            "episode": episode or prev.get("episode") or "",
        }
    persist(force=True)


def _watched_entry(guid: str, bucket: dict | None = None) -> dict:
    """已看记录兼容旧格式（纯时间戳）与新格式（含最后观看集）。"""
    raw = (bucket or _bucket())["watched"].get(guid)
    if isinstance(raw, dict):
        return raw
    if raw:
        return {"at": int(raw), "episode": ""}
    return {}


def remove_watched(guid: str) -> None:
    load()
    with _lock:
        _bucket()["watched"].pop(guid, None)
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
        bucket = _bucket()
        history = bucket["history"]
        prev = dict(history.get(guid) or {})
        if duration > 0 and ts >= duration * 0.95:
            history.pop(guid, None)
            bucket["watched"][guid] = {"at": int(time.time()),
                                       "episode": episode}
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
            history[guid] = prev
            _trim()
    persist(force=finished)


def delete_records(ids: list) -> None:
    load()
    with _lock:
        history = _bucket()["history"]
        for guid in ids:
            history.pop(guid, None)
    persist(force=True)


def history_records() -> list:
    load()
    with _lock:
        rows = [(guid, dict(rec)) for guid, rec in _bucket()["history"].items()]
    rows.sort(key=lambda pair: pair[1].get("updated_at", 0), reverse=True)
    return rows


def is_favorite(guid: str) -> bool:
    load()
    with _lock:
        return guid in _bucket()["favorites"]


def watched_ts(guid: str) -> int:
    """已看标记时间（unix 秒），不是播放进度。进度请用 ts_for。"""
    load()
    with _lock:
        return int(_watched_entry(guid).get("at") or 0)


def ts_for(guid: str) -> int:
    """续播位置（秒）。官方客户端的 play/info 就是拿这个值 seek。"""
    load()
    with _lock:
        return int((_bucket()["history"].get(guid) or {}).get("ts") or 0)


def episode_for(guid: str) -> str:
    load()
    with _lock:
        return str((_bucket()["history"].get(guid) or {}).get("episode_guid")
                   or "")


def episode_ts(guid: str, episode: str) -> int:
    """某集的进度，只在该集是最后观看的一集时返回。"""
    load()
    with _lock:
        rec = _bucket()["history"].get(guid) or {}
        if str(rec.get("episode_guid") or "") != (episode or ""):
            return 0
        return int(rec.get("ts") or 0)


def media_for(guid: str) -> str:
    load()
    with _lock:
        return str((_bucket()["history"].get(guid) or {}).get("media_guid") or "")


def duration_for(guid: str) -> int:
    load()
    with _lock:
        return int((_bucket()["history"].get(guid) or {}).get("duration") or 0)


def apply(row: dict, guid: str, rec_guid: str | None = None) -> dict:
    """把本地状态回填到官方 item 结构。

    `watched_ts` 在官方接口里是「上次播放到第几秒」（客户端拿它 seek），
    不是「标记已看的时间」。已看时间只放进 watched_at，避免客户端把
    unix 时间戳当成播放位置跳到片尾。
    """
    key = rec_guid or guid
    load()
    with _lock:
        bucket = _bucket()
        fav = guid in bucket["favorites"] or key in bucket["favorites"]
        stamp = int(_watched_entry(guid, bucket).get("at")
                    or _watched_entry(key, bucket).get("at") or 0)
        rec = dict(bucket["history"].get(key) or {})
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
