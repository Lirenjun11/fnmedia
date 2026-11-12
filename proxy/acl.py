#!/usr/bin/env python3
"""按 fnOS 账号做在线内容授权：身份识别 + 库/源授权 + 请求期裁剪。

设计约束（见 ``docs/source-acl.md``）：

- **默认关闭**：``acl_enabled=false`` 时本模块只保留原有的「源已禁用即失效」
  判定，其它逻辑全部短路，行为与旧版完全一致。
- **默认拒绝**：开启后，未识别账号、未授权库/源一律不可见；识别失败不静默。
- **管理页豁免**：``/v/api/ext/*`` 与 ``/v/online-admin`` 永不参与 ACL，
  保证任何时候都能关闭。
- **只读**：官方 sqlite 一律 ``mode=ro``，只取 user_id 与展示名；凭证只做
  哈希缓存，不落盘、不进日志。

身份来源（按优先级）：

1. 网关/客户端注入的用户头（``x-user-id`` 等），值直接当账号 id；
2. 管理页「绑定当前设备」：把当前请求的凭证指纹映射到账号（最可靠，
   不依赖官方 token 格式）；
3. 官方 sqlite 会话表：拿 Cookie/Authorization 值（含 md5/sha256 形式）
   在会话表里反查 user_id（尽力而为，探测不到就降级为未识别）。
"""
from __future__ import annotations

import contextvars
import hashlib
import os
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass

import borrow
import config
import runtime

# 可能携带「当前账号」的请求头：有的网关会注入，值是账号 id 或用户名。
HEADER_NAMES = (
    "x-user-id", "x-userid", "x-uid", "x-user",
    "x-trim-user-id", "trim-user-id", "x-trim-uid",
    "x-fnos-user-id", "x-fnos-userid", "x-fnos-uid",
)
# Cookie 里可能是会话凭证的键；找不到就整串 Cookie 参与哈希匹配。
COOKIE_NAMES = (
    "fnos-token", "fnos_token", "fnos-token-v2", "trim-token", "trim_token",
    "fnnas-token", "token", "access_token", "session", "sessionid", "sid",
)

_CACHE_TTL = 30.0
_DISCOVER_TTL = 120.0
_LOG_TTL = 300.0
_LOCK = threading.RLock()
_ID_CACHE: dict[str, tuple[float, "Identity"]] = {}
_LOG_SEEN: dict[str, tuple[float, str]] = {}
_DISCOVER_CACHE: tuple[float, dict] | None = None

_UID_COLS = ("uid", "user_id", "userid")
_NAME_COLS = ("nickname", "nick_name", "username", "user_name", "name",
              "display_name", "account")
_TOKEN_HINTS = ("token", "session", "sid", "ticket")


@dataclass(frozen=True)
class Identity:
    uid: str = ""
    name: str = ""
    via: str = ""          # off | header | device | session | unknown
    reason: str = ""       # 未识别原因，用于日志与前端横幅
    fp: str = ""           # 设备指纹（哈希），仅诊断用

    @property
    def known(self) -> bool:
        return bool(self.uid)


_UNKNOWN = Identity(via="unknown", reason="no-context")
_current: contextvars.ContextVar[Identity] = contextvars.ContextVar(
    "fnmedia_acl_identity", default=_UNKNOWN)


# --------------------------------------------------------------- 开关

def enabled() -> bool:
    return bool(config.get().get("acl_enabled"))


def current() -> Identity:
    return _current.get()


def push(value: Identity):
    return _current.set(value)


def reset(token) -> None:
    try:
        _current.reset(token)
    except (ValueError, LookupError):
        pass


# --------------------------------------------------------------- 凭证

def _ua(request) -> str:
    try:
        return str(request.headers.get("user-agent") or "")[:120]
    except Exception:  # noqa: BLE001 - 诊断用途，任何异常都不影响主链
        return ""


def credential(request) -> str:
    """能把「同一台设备」稳定串起来的原始凭证串。

    优先 ``Authorization``（App/客户端）。Cookie 只取与登录相关的键，避免
    播放器/页面自设的临时 cookie 改变指纹导致绑定失效；一个都没命中时才
    退回整串 Cookie。两者都没有时退回 IP + UA（最弱，仅用于诊断与手动绑定）。
    """
    auth = ""
    cookie = ""
    try:
        auth = str(request.headers.get("authorization") or "")
        cookie = str(request.headers.get("cookie") or "")
    except Exception:  # noqa: BLE001
        pass
    picked = []
    for part in cookie.split(";"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip().lower()
        value = value.strip().strip('"')
        if key in COOKIE_NAMES and value:
            picked.append(f"{key}={value}")
    if picked:
        return auth + "\n" + ";".join(sorted(picked))
    if auth or cookie:
        return auth + "\n" + cookie
    host = ""
    try:
        host = request.client.host if request.client else ""
    except Exception:  # noqa: BLE001
        pass
    return f"ip:{host}\nua:{_ua(request)}"


def device_fp(request) -> str:
    raw = credential(request)
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:24]


def _candidates(request) -> list[str]:
    """请求里可能是会话凭证的值（原值；调用方再补 md5/sha256 变体）。"""
    out: list[str] = []
    try:
        auth = str(request.headers.get("authorization") or "")
    except Exception:  # noqa: BLE001
        auth = ""
    if auth:
        value = auth.split(None, 1)[1] if " " in auth else auth
        if value:
            out.append(value.strip())
    try:
        cookie = str(request.headers.get("cookie") or "")
    except Exception:  # noqa: BLE001
        cookie = ""
    for part in cookie.split(";"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip().lower()
        value = value.strip().strip('"')
        if not value:
            continue
        if key in COOKIE_NAMES or len(value) >= 24:
            out.append(value)
    # 去重且限制数量，避免把超长 Cookie 全丢进 SQL。
    seen, clean = set(), []
    for value in out:
        if value in seen or len(value) > 4096:
            continue
        seen.add(value)
        clean.append(value)
    return clean[:8]


def _variants(value: str) -> list[str]:
    token = value.strip()
    out = [token]
    try:
        out.append(hashlib.md5(token.encode()).hexdigest())
        out.append(hashlib.sha256(token.encode()).hexdigest())
    except Exception:  # noqa: BLE001
        pass
    return out


# --------------------------------------------------------------- 官方库探测

def _db_files() -> list[str]:
    dirs = list(borrow.CANDIDATES)
    dirs += [f"/vol{n}/@appdata/trim.media/database" for n in range(3, 9)]
    found: list[str] = []
    for directory in dict.fromkeys(dirs):
        if not os.path.isdir(directory):
            continue
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names[:24]:
            if name.endswith((".db", ".sqlite", ".sqlite3")):
                found.append(os.path.join(directory, name))
    return found


def _schema(conn: sqlite3.Connection) -> dict[str, list[str]]:
    tables: dict[str, list[str]] = {}
    for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"):
        name = str(row[0])
        try:
            cols = [str(r[1]) for r in conn.execute(
                f'PRAGMA table_info("{name}")')]
        except sqlite3.Error:
            continue
        tables[name] = cols
    return tables


def _user_table(tables: dict[str, list[str]]) -> tuple[str, str, str] | None:
    for name, cols in tables.items():
        low = name.lower()
        if "user" not in low and "account" not in low:
            continue
        lowered = {c.lower(): c for c in cols}
        id_col = next((lowered[c] for c in _UID_COLS if c in lowered), "")
        name_col = next((lowered[c] for c in _NAME_COLS if c in lowered), "")
        if id_col and name_col:
            return name, id_col, name_col
    return None


def _session_tables(tables: dict[str, list[str]]) -> list[tuple[str, list[str], str]]:
    out = []
    for name, cols in tables.items():
        lowered = {c.lower(): c for c in cols}
        token_cols = [c for low, c in lowered.items()
                      if any(h in low for h in _TOKEN_HINTS)]
        uid_col = next((lowered[c] for c in _UID_COLS if c in lowered), "")
        if token_cols and uid_col:
            out.append((name, token_cols[:6], uid_col))
    return out


def discover(force: bool = False) -> dict:
    """只读探测官方用户表/会话表；结果缓存，失败不留异常。"""
    global _DISCOVER_CACHE
    now = time.monotonic()
    with _LOCK:
        if not force and _DISCOVER_CACHE and now - _DISCOVER_CACHE[0] < _DISCOVER_TTL:
            return _DISCOVER_CACHE[1]
    report: dict = {"db": "", "users": [], "tables": [], "error": ""}
    users: list[dict] = []
    tables_seen: list[dict] = []
    for path in _db_files():
        try:
            with closing(borrow._connect(path)) as conn:  # noqa: SLF001 - 复用只读连接
                tables = _schema(conn)
                user_table = _user_table(tables)
                sessions = _session_tables(tables)
                tables_seen.append({
                    "db": os.path.basename(path),
                    "user_table": user_table[0] if user_table else "",
                    "session_tables": [t[0] for t in sessions],
                })
                if user_table:
                    table, id_col, name_col = user_table
                    rows = conn.execute(
                        f'SELECT "{id_col}", "{name_col}" FROM "{table}" LIMIT 200'
                    ).fetchall()
                    for row in rows:
                        uid = str(row[0] if row[0] is not None else "").strip()
                        if not uid:
                            continue
                        users.append({
                            "id": uid[:64],
                            "name": str(row[1] or uid).strip()[:60] or uid,
                            "source": "auto",
                        })
                    if not report["db"]:
                        report["db"] = path
        except (sqlite3.Error, OSError) as exc:
            report["error"] = report["error"] or type(exc).__name__
            continue
    # 去重
    seen: set[str] = set()
    clean = []
    for row in users:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        clean.append(row)
    report["users"] = clean[:200]
    report["tables"] = tables_seen[:12]
    with _LOCK:
        _DISCOVER_CACHE = (now, report)
    return report


def _lookup_session(request) -> tuple[str, str]:
    """用请求凭证在官方会话表里反查 user_id；返回 (uid, 原因)。"""
    values = _candidates(request)
    if not values:
        return "", "no_credential"
    report = discover()
    if not any((row or {}).get("session_tables")
               for row in report.get("tables") or []):
        # 连会话表都没探测到，别再逐请求扫库，直接给出可诊断的原因。
        return "", "no_session_table"
    wanted: list[str] = []
    for value in values:
        wanted.extend(_variants(value))
    wanted = list(dict.fromkeys(wanted))[:24]
    placeholders = ",".join("?" for _ in wanted)
    for path in _db_files():
        try:
            with closing(borrow._connect(path)) as conn:  # noqa: SLF001
                tables = _schema(conn)
                for table, token_cols, uid_col in _session_tables(tables):
                    for token_col in token_cols:
                        sql = (f'SELECT "{uid_col}" FROM "{table}" '
                               f'WHERE "{token_col}" IN ({placeholders}) LIMIT 1')
                        try:
                            row = conn.execute(sql, wanted).fetchone()
                        except sqlite3.Error:
                            continue
                        if row and str(row[0] or "").strip():
                            return str(row[0]).strip()[:64], ""
        except (sqlite3.Error, OSError):
            continue
    return "", "no_mapping"


# --------------------------------------------------------------- 身份解析

def _subject_name(uid: str) -> str:
    for row in config.get().get("acl_subjects") or []:
        if str(row.get("id") or "") == uid:
            return str(row.get("name") or uid)
    for row in discover().get("users") or []:
        if str(row.get("id") or "") == uid:
            return str(row.get("name") or uid)
    return uid


def _log_ident(ident: Identity) -> None:
    """同一设备同一结果 5 分钟内只记一次，避免每个媒体分片都刷日志。"""
    fp = ident.fp or ""
    now = time.monotonic()
    with _LOCK:
        last = _LOG_SEEN.get(fp)
        if last and last[1] == ident.uid and now - last[0] < _LOG_TTL:
            return
        if len(_LOG_SEEN) > 500:
            _LOG_SEEN.clear()
        _LOG_SEEN[fp] = (now, ident.uid)
    if ident.known:
        runtime.log(f"acl identify: via={ident.via} uid={ident.uid} "
                    f"fp={fp[:8]}")
    else:
        runtime.log(f"acl identify: unknown reason={ident.reason} "
                    f"fp={fp[:8]}")


def identify(request, force: bool = False) -> Identity:
    """解析当前请求属于哪个账号；失败返回带原因的 unknown。

    ``force=True`` 用于管理页「开启前自检」：即使总开关还是关闭的，也实际
    走一遍身份映射，让管理员先确认能不能识别，再决定是否开启。
    """
    if not enabled() and not force:
        return Identity(via="off", fp=device_fp(request))
    cache_key = device_fp(request)
    now = time.monotonic()
    with _LOCK:
        hit = _ID_CACHE.get(cache_key) if cache_key else None
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]

    cfg = config.get()
    ident: Identity
    # 1) 显式用户头：最短路径，也最容易被网关覆盖。
    picked = ""
    for name in HEADER_NAMES:
        try:
            value = request.headers.get(name)
        except Exception:  # noqa: BLE001
            value = None
        if value and len(value.strip()) <= 64:
            picked = value.strip()
            break
    if picked:
        ident = Identity(uid=picked, name=_subject_name(picked), via="header",
                         fp=cache_key)
        _log_ident(ident)
    else:
        # 2) 管理页绑定的设备指纹。
        rec = (cfg.get("acl_devices") or {}).get(cache_key)
        if isinstance(rec, dict) and rec.get("uid"):
            uid = str(rec["uid"])
            ident = Identity(uid=uid, name=_subject_name(uid), via="device",
                             fp=cache_key)
            _log_ident(ident)
        else:
            # 3) 官方库会话表反查（尽力而为）。
            uid, reason = _lookup_session(request)
            if uid:
                ident = Identity(uid=uid, name=_subject_name(uid), via="session",
                                 fp=cache_key)
                _log_ident(ident)
            else:
                ident = Identity(via="unknown", reason=reason or "no_mapping",
                                 fp=cache_key)
                _log_ident(ident)
    if cache_key:
        with _LOCK:
            if len(_ID_CACHE) > 500:
                _ID_CACHE.clear()
            _ID_CACHE[cache_key] = (now, ident)
    return ident


def invalidate(uid: str = "") -> None:
    """授权/绑定变更后清掉身份缓存，立即生效。"""
    with _LOCK:
        if not uid:
            _ID_CACHE.clear()
            return
        for key, (_, ident) in list(_ID_CACHE.items()):
            if ident.uid == uid:
                _ID_CACHE.pop(key, None)


# --------------------------------------------------------------- 授权判定

def _grant(uid: str) -> dict:
    if not uid:
        return {}
    value = (config.get().get("acl_grants") or {}).get(uid)
    return value if isinstance(value, dict) else {}


def granted_sources(uid: str) -> set[str]:
    value = _grant(uid).get("sources")
    if not isinstance(value, list):
        return set()
    return {str(x) for x in value if str(x).strip()}


def granted_libs(uid: str) -> set[str]:
    value = _grant(uid).get("libs")
    if not isinstance(value, list):
        return set()
    return {str(x) for x in value if str(x).strip()}


def sources(ignore_acl: bool = False) -> list[dict]:
    """当前主体可用于搜索/播放的源（ACL 关闭时等于全部启用源）。

    ``ignore_acl=True`` 只给媒体分片链路用：播放器拉 m3u8/分片时不一定带
    登录凭证，按身份裁剪会把整条播放打断。媒体 URL 的可播放性由入口
    （play/play、media/range）在带凭证时决定。
    """
    pool = config.enabled_sources()
    if ignore_acl or not enabled():
        return pool
    allowed = granted_sources(current().uid)
    return [s for s in pool if str(s.get("id") or "") in allowed]


def provider_allowed(pid: str) -> bool:
    """单条线路的来源是否可用：全局启用 + 当前主体已授权。"""
    pid = str(pid or "")
    if not config.source_live(pid):
        return False
    if not enabled():
        return True
    if not pid:
        return False
    return pid in granted_sources(current().uid)


def lib_allowed(key: str) -> bool:
    if not enabled():
        return True
    key = str(key or "")
    if not key:
        return False
    libs = granted_libs(current().uid)
    return "*" in libs or key in libs


def filter_brief(brief: dict | None, enforce: bool = True) -> dict | None:
    """按当前主体裁剪 brief 的线路；无可用线路返回 None。

    ACL 关闭时退化成「源被禁用即失效」，与旧版 ``_brief`` 行为一致。
    ``enforce=False`` 用于媒体分片链路：只校验源是否仍启用，不按身份裁剪，
    避免播放器后续请求不带登录凭证时被误杀（这些 URL 的可播性由起播入口
    在带凭证时决定）。
    """
    if not isinstance(brief, dict):
        return None
    if not enabled() or not enforce:
        return brief if config.source_live(brief.get("provider")) else None
    allowed = granted_sources(current().uid)
    mapping = brief.get("line_sources")
    if not isinstance(mapping, dict):
        mapping = {}
    lines = brief.get("lines") or {}
    names = [str(n) for n in (brief.get("line_names") or []) if n]
    keep: list[str] = []
    for name in names:
        src = str(mapping.get(name) or brief.get("provider") or "")
        if not config.source_live(src):
            continue
        if not src or src not in allowed:
            continue
        if not lines.get(name):
            continue
        keep.append(name)
    if not keep:
        return None
    if len(keep) == len(names) and mapping:
        return brief
    out = dict(brief)
    out["lines"] = {name: lines[name] for name in keep}
    out["line_names"] = keep
    out["episodes"] = max((len(lines[name]) for name in keep), default=1)
    return out


# --------------------------------------------------------------- 管理视图

def subjects() -> list[dict]:
    """授权主体清单：配置里的 + 官方库自动发现的，id 去重。"""
    out: list[dict] = []
    seen: set[str] = set()
    for row in config.get().get("acl_subjects") or []:
        uid = str(row.get("id") or "")
        if not uid or uid in seen:
            continue
        seen.add(uid)
        out.append({"id": uid, "name": str(row.get("name") or uid),
                    "source": str(row.get("source") or "manual")})
    for row in discover().get("users") or []:
        uid = str(row.get("id") or "")
        if not uid or uid in seen:
            continue
        seen.add(uid)
        out.append({"id": uid, "name": str(row.get("name") or uid),
                    "source": "auto"})
    # 授权表里有、但主体清单暂时读不到的账号也要能管理（D3：两通道解耦）。
    for uid in (config.get().get("acl_grants") or {}):
        if uid and uid not in seen:
            seen.add(uid)
            out.append({"id": uid, "name": uid, "source": "manual"})
    for rec in (config.get().get("acl_devices") or {}).values():
        uid = str((rec or {}).get("uid") or "")
        if uid and uid not in seen:
            seen.add(uid)
            out.append({"id": uid, "name": uid, "source": "manual"})
    return out[:200]


def status() -> dict:
    """管理页只读状态：不含任何凭证明文。"""
    cfg = config.get()
    ident = current()
    report = discover()
    grants = cfg.get("acl_grants") or {}
    subjects_list = subjects()
    granted_any: set[str] = set()
    for value in grants.values():
        if isinstance(value, dict):
            granted_any |= {str(x) for x in (value.get("sources") or [])}
    enabled_ids = [str(s.get("id") or "") for s in config.enabled_sources()]
    unassigned_sources = [sid for sid in enabled_ids if sid not in granted_any]
    unassigned_subjects = [row["id"] for row in subjects_list
                           if not (grants.get(row["id"]) or {}).get("libs")
                           and not (grants.get(row["id"]) or {}).get("sources")]
    return {
        "enabled": enabled(),
        "subjects": subjects_list,
        "grants": grants,
        "devices": [
            {"fp": fp, "uid": rec.get("uid"), "ua": rec.get("ua"),
             "ts": rec.get("ts")}
            for fp, rec in (cfg.get("acl_devices") or {}).items()
        ],
        "me": {
            "uid": ident.uid, "name": ident.name, "via": ident.via,
            "reason": ident.reason, "fp": ident.fp,
            "known": ident.known,
        },
        "discover": {
            "db": report.get("db") or "",
            "users": len(report.get("users") or []),
            "tables": report.get("tables") or [],
            "error": report.get("error") or "",
        },
        "unassigned_sources": len(unassigned_sources),
        "unassigned_subjects": len(unassigned_subjects),
        "enabled_sources": len(enabled_ids),
    }


def probe(request) -> dict:
    """管理页自检：当前请求实际带了什么、能识别成谁、官方库长什么样。"""
    fp = device_fp(request)
    with _LOCK:
        _ID_CACHE.pop(fp, None)
    ident = identify(request, force=True)
    headers = {}
    try:
        for name in HEADER_NAMES:
            value = request.headers.get(name)
            headers[name] = "present" if value else ""
    except Exception:  # noqa: BLE001
        pass
    cookie_names = []
    try:
        cookie = str(request.headers.get("cookie") or "")
        for part in cookie.split(";"):
            if "=" in part:
                cookie_names.append(part.split("=", 1)[0].strip())
    except Exception:  # noqa: BLE001
        pass
    values = _candidates(request)
    report = discover(force=True)
    return {
        "me": {"uid": ident.uid, "name": ident.name, "via": ident.via,
               "reason": ident.reason, "fp": ident.fp, "known": ident.known},
        "headers": headers,
        "cookie_keys": cookie_names[:24],
        "credential_candidates": len(values),
        "discover": {
            "db": report.get("db") or "",
            "users": report.get("users") or [],
            "tables": report.get("tables") or [],
            "error": report.get("error") or "",
        },
    }
