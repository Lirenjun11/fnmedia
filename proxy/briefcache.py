"""浏览过的条目 brief 落盘缓存（虚拟库 / 搜索结果用）。

`cms.TitleStore` 是**进程级内存**，服务重启（含 fnOS 应用中心升级安装）
就清空。播放历史那类条目由 ``playcache`` 按 playstate 引用恢复，但
**虚拟库里点开的条目**（榜单卡、源×分类卡、搜索结果卡）没有任何持久化：
客户端缓存了库里的列表，重启后再点同一张卡时 guid 反查不到 brief，
详情请求会原样透传给官方后端，拿到 ``code=-6 此媒体库不存在``——表现就是
「库能进，里面的条目一开就报错」；清缓存重建也救不回来，因为重建只恢复
路由，不恢复 brief。

这里把最近浏览/搜索到的 brief 落盘到 ``state/browse_titles.json``，启动时
按「源是否仍启用 + 是否过期」过滤后恢复进 store。只缓存元数据与线路地址，
不缓存任何媒体内容；源被删除/禁用即丢弃，避免重启后旧线路“复活”。
"""
from __future__ import annotations

import json
import threading
import time

import config
import runtime

_FILE = config.STATE_DIR / "browse_titles.json"
_MAX = 800               # 单条 brief 带线路地址，控制在 1~2 MB 量级
_TTL = 7 * 86400.0       # 超过一周的浏览记录不再恢复
_WRITE_THROTTLE = 20.0

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
        if not guid or not isinstance(brief, dict) or not brief.get("title"):
            continue
        # 老文件里带着完整分集（实测 800 条 135MB），加载时就地卸掉。
        if brief.get("lines"):
            brief = dict(brief)
            brief["lines"] = {}
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
    global _loaded, _items, _last_write
    with _LOCK:
        _items = {}
        _loaded = False
        _last_write = 0.0


def remember(briefs: list[dict]) -> None:
    """记录一批刚浏览到的条目（搜索结果、虚拟库列表都走这里）。"""
    rows = [b for b in (briefs or [])
            if isinstance(b, dict) and b.get("guid") and b.get("title")]
    if not rows:
        return
    with _LOCK:
        _load()
        now = time.time()
        for brief in rows:
            # 分集地址是内存大头（一条剧集平均 242 集 / 173KB）。这里只留
            # 元数据：guid 能反查到条目，卡片点开不会报「媒体库不存在」；
            # 真正要播时播放入口会按需补一次详情拿到线路。
            slim = dict(brief)
            slim["lines"] = {}
            _items[str(brief["guid"])] = {"ts": now, "brief": slim}
        if len(_items) > _MAX:
            oldest = sorted(_items, key=lambda g: _items[g].get("ts", 0))
            for key in oldest[:len(_items) - _MAX]:
                _items.pop(key, None)
        _write()


def restore(store) -> int:
    """启动时恢复条目元数据；源已禁用或过期的直接丢弃。

    分集地址不落盘（太占内存），播放时由 ``_online_brief`` 按需补详情。
    """
    count = 0
    dropped = 0
    now = time.time()
    with _LOCK:
        _load()
        for guid in list(_items):
            rec = _items[guid] or {}
            brief = rec.get("brief") or {}
            if (now - float(rec.get("ts") or 0) > _TTL
                    or not config.source_live(brief.get("provider"))):
                _items.pop(guid, None)
                dropped += 1
                continue
            try:
                store.put_brief(dict(brief))
                count += 1
            except Exception:  # noqa: BLE001 - 单条脏数据不影响启动
                _items.pop(guid, None)
        if dropped:
            _write(force=True)
    return count
