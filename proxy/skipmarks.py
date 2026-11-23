#!/usr/bin/env python3
"""跳过片头/片尾标记（按账号 + 基础影片/季持久化）。

飞牛原生播放器只认 ``play/info`` 响应里的 ``play_config.skip_opening`` /
``skip_ending``（秒），并通过 ``POST /play/setConfigByItem`` 回写；本模块就是
这条链路的服务端存储，等价于官方 NAS 端播放器右下角那个「跳过片头/片尾」菜单。

几个硬约束（都有实际原因）：

1. **按账号分区**：与 ``playstate``（收藏/已看/进度）同一口径，子账号不会互相
   覆盖对方的跳过秒数；未识别身份落到全局分区 ``""``。
2. **按基础影片/季保存**：同一部剧换线路、换集都不需要重设。键由调用方归一化
   （见 ``app._skip_keys``），这里只按传入的字符串存；读侧支持候选键兜底，
   内存索引被淘汰时标记也不会「消失」。
3. **关闭只是下发 0**：``enabled=False`` 时 ``play_config`` 返回 0，已设秒数保留，
   重新打开不用重填。
4. 只下发原生字段，不塞客户端不认识的键——多出来的键会被当成播放配置透传。

落盘：``state/skip_marks.json``（独立文件，不和 config.json 抢写，避免与
playstate 的节流写互相覆盖）。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import config

_STATE = Path(config.STATE_DIR) / "skip_marks.json"
_LOCK = threading.RLock()
_MAX_MARKS = 500          # 每账号最多保留条数，超出按最后修改淘汰
_MAX_USERS = 50
MAX_SKIP_SECONDS = 600    # 原生跳过上限（10 分钟）


def _default() -> dict:
    return {"skip_opening": 0, "skip_ending": 0, "enabled": True,
            "updated_at": 0}


def _uid() -> str:
    """当前请求的账号；未识别返回 ""（全局分区）。"""
    try:
        import playstate
        return playstate.current_uid()
    except Exception:  # noqa: BLE001 - 身份解析失败不得影响播放
        return ""


def _num(value, default: int = 0) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(0, min(MAX_SKIP_SECONDS, n))


def _load() -> dict:
    try:
        data = json.loads(_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    users = data.get("users") if isinstance(data, dict) else None
    return users if isinstance(users, dict) else {}


def _save(users: dict) -> None:
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"users": users}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(_STATE)


def _keys(guid: str, aliases: tuple) -> list[str]:
    out: list[str] = []
    for value in (guid,) + tuple(aliases or ()):
        key = str(value or "")
        if key and key not in out:
            out.append(key)
    return out


def _find(mine: dict, keys: list[str]) -> dict:
    for key in keys:
        rec = mine.get(key)
        if isinstance(rec, dict):
            return rec
    return {}


def _merge(raw: dict) -> dict:
    out = _default()
    if isinstance(raw, dict):
        out["skip_opening"] = _num(raw.get("skip_opening"))
        out["skip_ending"] = _num(raw.get("skip_ending"))
        out["enabled"] = raw.get("enabled") is not False
        out["updated_at"] = int(raw.get("updated_at") or 0)
    return out


def get(guid: str, *aliases: str) -> dict:
    """读标记：先按 guid，再按候选键（季 guid / 集 guid）。"""
    keys = _keys(guid, aliases)
    if not keys:
        return _default()
    with _LOCK:
        users = _load()
    mine = users.get(_uid()) or {}
    return _merge(_find(mine if isinstance(mine, dict) else {}, keys))


def set_mark(guid: str, payload: dict) -> dict:
    """写入标记；只更新 payload 里出现的字段，其余保持原值。"""
    key = str(guid or "").strip()
    if not key:
        raise ValueError("缺少 guid")
    body = payload if isinstance(payload, dict) else {}
    mark = get(key)
    if "skip_opening" in body:
        mark["skip_opening"] = _num(body.get("skip_opening"))
    if "skip_ending" in body:
        mark["skip_ending"] = _num(body.get("skip_ending"))
    if "enabled" in body:
        mark["enabled"] = body.get("enabled") is not False
    mark["updated_at"] = int(time.time() * 1000)
    uid = _uid()
    with _LOCK:
        users = _load()
        mine = dict(users.get(uid) or {})
        mine[key] = mark
        users[uid] = _trim(mine)
        _trim_users(users)
        _save(users)
    return mark


def _trim(mine: dict) -> dict:
    if len(mine) <= _MAX_MARKS:
        return mine
    keep = sorted(mine, key=lambda k: int((mine[k] or {}).get("updated_at") or 0),
                  reverse=True)[:_MAX_MARKS]
    return {k: mine[k] for k in keep}


def _trim_users(users: dict) -> None:
    if len(users) <= _MAX_USERS:
        return
    def last(uid: str) -> int:
        return max([int((v or {}).get("updated_at") or 0)
                    for v in (users.get(uid) or {}).values()] + [0])
    ranked = sorted(users, key=last, reverse=True)
    for uid in ranked[_MAX_USERS:]:
        users.pop(uid, None)


def delete(guid: str, *aliases: str) -> None:
    keys = _keys(guid, aliases)
    if not keys:
        return
    uid = _uid()
    with _LOCK:
        users = _load()
        if uid not in users:
            return
        mine = dict(users.get(uid) or {})
        for key in keys:
            mine.pop(key, None)
        users[uid] = mine
        _save(users)


def play_config(guid: str, *aliases: str) -> dict:
    """下发给原生播放器的 play_config；禁用或未设置时均为 0。"""
    mark = get(guid, *aliases)
    if not mark["enabled"]:
        return {"guid": str(guid or ""), "skip_opening": 0, "skip_ending": 0}
    return {"guid": str(guid or ""), "skip_opening": mark["skip_opening"],
            "skip_ending": mark["skip_ending"]}
