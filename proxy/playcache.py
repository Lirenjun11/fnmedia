"""最近播放 / 收藏条目的 brief 落盘缓存。

`cms.TitleStore` 是**进程级内存**，服务重启（包括 fnOS 应用中心升级安装）
就清空；而 `play/list`、`favorite/list`、`item/{guid}` 都靠 store 反查 brief，
反查不到就把记录整条跳过。结果：升级或重启后「最近播放 / 收藏」列表变空，
点进去也没有线路。

这里把播放过、收藏过的条目 brief 持久化到 ``state/play_titles.json``，启动时
恢复进 store。只保留 playstate 里仍被引用的条目；源被删除或被禁用则丢弃，
避免重启后旧线路“复活”。
"""
from __future__ import annotations

import json
import threading
import time

import config
import runtime

_FILE = config.STATE_DIR / "play_titles.json"
_MAX = 300
_WRITE_THROTTLE = 30.0

_LOCK = threading.RLock()
_items: dict[str, dict] = {}      # guid -> {"ts": float, "brief": dict}
_loaded = False
_last_write = 0.0


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        raw = json.loads(_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    data = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(data, dict):
        return
    for guid, rec in list(data.items())[:_MAX]:
        brief = (rec or {}).get("brief") if isinstance(rec, dict) else None
        if not guid or not isinstance(brief, dict):
            continue
        _items[str(guid)] = {"ts": float(rec.get("ts") or 0), "brief": brief}


def _write(force: bool = False) -> None:
    global _last_write
    now = time.monotonic()
    with _LOCK:
        if not force and now - _last_write < _WRITE_THROTTLE:
            return
        _last_write = now
        payload = {"updated_at": int(time.time()), "items": _items}
        try:
            _FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False,
                                      separators=(",", ":")), encoding="utf-8")
            tmp.replace(_FILE)
        except OSError:
            pass


def reset() -> None:
    """测试用：丢弃内存态并从磁盘重新加载。"""
    global _loaded, _items
    with _LOCK:
        _items = {}
        _loaded = False


def remember(brief: dict) -> None:
    """记录一条可播放数据（播放、收藏、标记已看时调用）。"""
    guid = str((brief or {}).get("guid") or "")
    if not guid:
        return
    with _LOCK:
        _load()
        fresh = guid not in _items
        _items[guid] = {"ts": time.time(), "brief": dict(brief)}
        if len(_items) > _MAX:
            oldest = sorted(_items, key=lambda g: _items[g].get("ts", 0))
            for key in oldest[:len(_items) - _MAX]:
                _items.pop(key, None)
        _write(force=fresh)


def forget(guids: list[str]) -> None:
    if not guids:
        return
    with _LOCK:
        _load()
        removed = False
        for guid in guids:
            if _items.pop(str(guid), None) is not None:
                removed = True
        if removed:
            _write(force=True)


def ensure(store, guid: str) -> bool:
    """store 里没有这条数据时，从落盘缓存补回（覆盖内存条目被淘汰的情况）。"""
    if not guid or store.get(guid):
        return False
    with _LOCK:
        _load()
        rec = _items.get(guid)
        if not rec:
            return False
        brief = rec.get("brief")
        if not isinstance(brief, dict):
            return False
        if not config.source_live(brief.get("provider")):
            _items.pop(guid, None)
            _write(force=True)
            return False
    store.put_brief(dict(brief))
    return True


def restore(store) -> int:
    """启动时把缓存恢复到 store；按 playstate 引用与源存活状态清理。"""
    import playstate

    keep = playstate.referenced_guids()
    dropped = 0
    count = 0
    with _LOCK:
        _load()
        for guid in list(_items):
            brief = _items[guid].get("brief") or {}
            if guid not in keep or not config.source_live(brief.get("provider")):
                _items.pop(guid, None)
                dropped += 1
                continue
            store.put_brief(dict(brief))
            count += 1
        if dropped:
            _write(force=True)
    if dropped:
        runtime.log(f"playcache restore: 丢弃 {dropped} 条失效缓存")
    return count


def count() -> int:
    with _LOCK:
        _load()
        return len(_items)
