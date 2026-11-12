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
2. 官方登录会话：请求里的 token（``Authorization`` / ``Trim-MC-token``）
   在 ``trimactivity.db.user_token`` 里映射到 ``user_guid``，再回
   ``trimmedia.db.user`` 取用户名 —— **全自动，不用管理员配任何东西**；
3. 管理页「绑定当前设备」：前两步都识别不出来时的兜底（例如播放器进程
   没带 token）。
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
# 官方客户端把登录 token 放在这些位置（飞牛影视用的是 ``Trim-MC-token``）。
TOKEN_HEADERS = ("authorization", "trim-mc-token", "x-trim-token")
# Cookie 里可能是会话凭证的键；找不到就整串 Cookie 参与哈希匹配。
COOKIE_NAMES = (
    "trim-mc-token", "fnos-token", "fnos_token", "fnos-token-v2",
    "trim-token", "trim_token", "fnnas-token", "token", "access_token",
    "session", "sessionid", "sid",
)

_CACHE_TTL = 30.0
_DISCOVER_TTL = 120.0
_LOG_TTL = 300.0
_LOCK = threading.RLock()
_ID_CACHE: dict[str, tuple[float, "Identity"]] = {}
_LOG_SEEN: dict[str, tuple[float, str]] = {}
_DISCOVER_CACHE: tuple[float, dict] | None = None

# 官方库的主键列名：``trimmedia.db.user`` 用 ``guid``，会话表用 ``user_guid``。
# 上一版只匹配 uid/user_id，导致官方表整张识别不到、只能手工加账号。
_UID_COLS = ("guid", "user_guid", "userguid", "uid", "user_id", "userid")
_NAME_COLS = ("username", "nickname", "nick_name", "user_name", "name",
              "display_name", "account")
_USER_TABLES = ("user", "users", "account", "accounts")
# 会话表里判定「这条 token 还有效」与「取最新一条」的列。
_STATUS_COLS = ("status", "state", "valid", "enabled")
_ORDER_COLS = ("update_time", "updated_at", "mtime", "create_time", "id")


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
    """请求里可能是会话 token 的值，按可信度排序（原值，不做哈希）。

    官方做法是拿 ``Authorization`` / ``Trim-MC-token`` 的值**原样**去
    ``user_token.token`` 里比对，所以这里保持原值；带 ``Bearer`` 前缀的
    客户端再补一个去前缀的变体。
    """
    out: list[str] = []
    for name in TOKEN_HEADERS:
        try:
            raw = str(request.headers.get(name) or "").strip()
        except Exception:  # noqa: BLE001
            raw = ""
        if not raw:
            continue
        out.append(raw)
        if " " in raw:
            tail = raw.split(None, 1)[1].strip()
            if tail:
                out.append(tail)
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
        if value and key in COOKIE_NAMES:
            out.append(value)
    seen, clean = set(), []
    for value in out:
        if value in seen or len(value) > 4096:
            continue
        seen.add(value)
        clean.append(value)
    return clean[:8]


def _hash_variants(values: list[str]) -> list[str]:
    """少数部署会把 token 哈希后入库；原值匹配失败时再试一轮。"""
    out: list[str] = []
    for value in values[:4]:
        try:
            out.append(hashlib.md5(value.encode()).hexdigest())
            out.append(hashlib.sha256(value.encode()).hexdigest())
        except Exception:  # noqa: BLE001
            continue
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
        # 官方固定两个库：账号在 trimmedia.db，token→账号映射在
        # trimactivity.db。先排它们，避免被同目录其它 .db 抢在前面。
        ordered = [n for n in ("trimmedia.db", "trimactivity.db") if n in names]
        ordered += [n for n in names[:32] if n not in ordered
                    and n.endswith((".db", ".sqlite", ".sqlite3"))]
        found += [os.path.join(directory, n) for n in ordered]
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


def _pick(lowered: dict[str, str], names: tuple[str, ...]) -> str:
    """按优先级从「小写列名→真实列名」里挑一个存在的。"""
    for name in names:
        if name in lowered:
            return lowered[name]
    return ""


def _user_table(tables: dict[str, list[str]]) -> tuple[str, str, str] | None:
    """找出官方账号表；``user``/``users`` 等标准名字优先于「名字里带 user」。"""
    def match(name: str, cols: list[str]):
        lowered = {c.lower(): c for c in cols}
        id_col = _pick(lowered, _UID_COLS)
        name_col = _pick(lowered, _NAME_COLS)
        if id_col and name_col:
            return name, id_col, name_col
        return None

    for wanted in _USER_TABLES:
        for name, cols in tables.items():
            if name.lower() == wanted:
                hit = match(name, cols)
                if hit:
                    return hit
    for name, cols in tables.items():
        low = name.lower()
        if "user" not in low and "account" not in low:
            continue
        hit = match(name, cols)
        if hit:
            return hit
    return None


def _session_tables(tables: dict[str, list[str]]) -> list[dict]:
    """找出「token → 账号」映射表，并记录可用列。

    官方是 ``trimactivity.db.user_token(token, user_guid, status,
    update_time)``；这里不硬编码表名，只要求「有 token 列 + 有账号列」，
    这样官方以后改表名也能自动跟上。
    """
    out: list[dict] = []
    for name, cols in tables.items():
        lowered = {c.lower(): c for c in cols}
        token_cols = [c for low, c in lowered.items()
                      if low == "token" or low.endswith("_token")
                      or low.startswith("token_")]
        uid_col = _pick(lowered, _UID_COLS)
        if not token_cols or not uid_col:
            continue
        out.append({
            "table": name,
            "token_cols": token_cols[:4],
            "uid_col": uid_col,
            "status_col": _pick(lowered, _STATUS_COLS),
            "order_col": _pick(lowered, _ORDER_COLS),
            # 名字里直接带 token 的表（user_token）排前面，少打几次空查询。
            "rank": 0 if "token" in name.lower() else 1,
        })
    out.sort(key=lambda row: row["rank"])
    return out


def discover(force: bool = False) -> dict:
    """只读探测官方账号表/会话表；结果缓存，失败不留异常。"""
    global _DISCOVER_CACHE
    now = time.monotonic()
    with _LOCK:
        if not force and _DISCOVER_CACHE and now - _DISCOVER_CACHE[0] < _DISCOVER_TTL:
            return _DISCOVER_CACHE[1]
    report: dict = {"db": "", "users": [], "tables": [], "sessions": [],
                    "error": ""}
    users: list[dict] = []
    tables_seen: list[dict] = []
    sessions: list[dict] = []
    for path in _db_files():
        try:
            with closing(borrow._connect(path)) as conn:  # noqa: SLF001 - 复用只读连接
                tables = _schema(conn)
                user_table = _user_table(tables)
                found = _session_tables(tables)
                tables_seen.append({
                    "db": os.path.basename(path),
                    "user_table": user_table[0] if user_table else "",
                    "session_tables": [t["table"] for t in found],
                })
                for sess in found:
                    sessions.append(dict(sess, db=path))
                if user_table:
                    table, id_col, name_col = user_table
                    lowered = {c.lower(): c for c in tables[table]}
                    extra = [c for c in ("is_admin", "status")
                             if c in lowered]
                    cols = ", ".join(
                        [f'"{id_col}"', f'"{name_col}"']
                        + [f'"{lowered[c]}"' for c in extra])
                    rows = conn.execute(
                        f'SELECT {cols} FROM "{table}" LIMIT 200').fetchall()
                    for row in rows:
                        uid = str(row[0] if row[0] is not None else "").strip()
                        name = str(row[1] or "").strip()
                        # 官方会放一个占位账号；删除账号会改名为
                        # ``<用户名>_deleted_<时间戳>``，这些都不该出现在
                        # 授权清单里（否则删了以后还挂在列表上）。
                        low = f"{uid}\n{name}".lower()
                        if (not uid or uid == "default-user-template"
                                or "_deleted_" in low):
                            continue
                        item = {
                            "id": uid[:64],
                            "name": name[:60] or uid,
                            "source": "auto",
                        }
                        if "is_admin" in extra:
                            item["is_admin"] = bool(
                                row[2 + extra.index("is_admin")])
                        if "status" in extra:
                            item["status"] = row[2 + extra.index("status")]
                        users.append(item)
                    if not report["db"]:
                        report["db"] = os.path.dirname(path)
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
    # 管理员排前面，方便管理页里先看到「自己」。
    clean.sort(key=lambda row: (not row.get("is_admin"), row["name"]))
    report["users"] = clean[:200]
    report["tables"] = tables_seen[:12]
    report["sessions"] = sessions[:8]
    with _LOCK:
        _DISCOVER_CACHE = (now, report)
    return report


def _lookup_session(request) -> tuple[str, str]:
    """用请求里的登录 token 在官方会话表反查 user_guid；返回 (uid, 原因)。"""
    tokens = _candidates(request)
    if not tokens:
        return "", "no_credential"
    report = discover()
    sessions = report.get("sessions") or []
    if not sessions:
        return "", "no_session_table"
    by_db: dict[str, list[dict]] = {}
    for sess in sessions:
        by_db.setdefault(str(sess.get("db") or ""), []).append(sess)
    # 先按原值匹配；全不中再试一轮哈希形式（非官方部署才可能用到）。
    for values in (tokens, _hash_variants(tokens)):
        wanted = list(dict.fromkeys(values))[:16]
        if not wanted:
            continue
        marks = ",".join("?" for _ in wanted)
        for path, group in by_db.items():
            try:
                with closing(borrow._connect(path)) as conn:  # noqa: SLF001
                    for sess in group:
                        table = sess.get("table") or ""
                        uid_col = sess.get("uid_col") or ""
                        if not table or not uid_col:
                            continue
                        for token_col in sess.get("token_cols") or []:
                            sql = (f'SELECT "{uid_col}" FROM "{table}" '
                                   f'WHERE "{token_col}" IN ({marks})')
                            if sess.get("status_col"):
                                sql += f' AND "{sess["status_col"]}"=1'
                            if sess.get("order_col"):
                                sql += f' ORDER BY "{sess["order_col"]}" DESC'
                            sql += " LIMIT 1"
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
    """同一设备同一结果 5 分钟内只记一次，避免每个媒体分片都刷日志。

    账号权限没开时不记：此时识别只是给「播放状态按账号隔离」用，不必刷屏。
    """
    if not enabled():
        return
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
    # 身份解析与权限开关解耦：即使没开账号权限，也要能认出「现在是哪个账号」
    # ——播放历史/续播按账号隔离依赖它。权限判定仍由 enabled() 单独把关。
    cache_key = device_fp(request)
    now = time.monotonic()

    # 1) 显式用户头：最短路径，也最容易被网关覆盖。
    #
    # 这一路**不进指纹缓存**：指纹由凭证/UA 算出，不含请求头，同一台机器上
    # 带不同 x-user-id 的请求会共用同一个 key，先到的身份会把后面的全都盖掉
    # （表现为两个账号串到同一份播放进度）。取值本身是纯内存操作，不需要缓存。
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
        return ident

    with _LOCK:
        hit = _ID_CACHE.get(cache_key) if cache_key else None
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]

    cfg = config.get()
    ident: Identity
    # 2) 官方登录 token → 官方会话表 → user_guid（全自动，无需配置）。
    uid, reason = _lookup_session(request)
    if uid:
        ident = Identity(uid=uid, name=_subject_name(uid), via="session",
                         fp=cache_key)
        _log_ident(ident)
    else:
        # 3) 管理页「绑定当前设备」：会话反查不出来时的兜底。
        rec = (cfg.get("acl_devices") or {}).get(cache_key)
        if isinstance(rec, dict) and rec.get("uid"):
            uid = str(rec["uid"])
            ident = Identity(uid=uid, name=_subject_name(uid), via="device",
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

def _grant(uid: str) -> dict | None:
    """该账号的显式配置；``None`` 表示「未单独配置」，按默认策略处理。

    这层区分是关键：``libs: []`` 是「一个都不给」，而完全没有 ``libs``
    键是「没配过、按默认放行」。旧实现把两者混成一个空数组，等于一开启
    就把没配过的账号全禁了。
    """
    if not uid:
        return None
    value = (config.get().get("acl_grants") or {}).get(uid)
    if isinstance(value, dict) and value:
        return value
    return None


def default_visible() -> bool:
    """未单独配置的账号默认是否可见虚拟库（默认放行，同参考项目）。"""
    return bool(config.get().get("acl_default_visible", True))


def default_online() -> bool:
    """未单独配置的账号默认是否可用在线搜索/播放。"""
    return bool(config.get().get("acl_default_online", True))


def granted_sources(uid: str) -> set[str] | None:
    """``None`` = 不限（全部启用源可用）；空集合 = 一条都不许。"""
    if not enabled():
        return None
    grant = _grant(uid) or {}
    if grant.get("online") is False:
        return set()
    if isinstance(grant.get("sources"), list):
        return {str(x) for x in grant["sources"] if str(x).strip()}
    if not default_online():
        return set()
    return None


def granted_libs(uid: str) -> set[str] | None:
    """``None`` = 按默认策略；空集合 = 一个虚拟库都看不到。"""
    grant = _grant(uid) or {}
    libs = grant.get("libs")
    if not isinstance(libs, list):
        return None
    return {str(x) for x in libs if str(x).strip()}


def sources(ignore_acl: bool = False) -> list[dict]:
    """当前主体可用于搜索/播放的源（ACL 关闭或「不限」时等于全部启用源）。

    ``ignore_acl=True`` 只给媒体分片链路用：播放器拉 m3u8/分片时不一定带
    登录凭证，按身份裁剪会把整条播放打断。媒体 URL 的可播放性由入口
    （play/play、media/range）在带凭证时决定。
    """
    pool = config.enabled_sources()
    if ignore_acl or not enabled():
        return pool
    allow = granted_sources(current().uid)
    if allow is None:
        return pool
    return [s for s in pool if str(s.get("id") or "") in allow]


def provider_allowed(pid: str) -> bool:
    """单条线路的来源是否可用：全局启用 + 当前主体策略允许。"""
    pid = str(pid or "")
    if not config.source_live(pid):
        return False
    if not enabled():
        return True
    if not pid:
        return False
    allow = granted_sources(current().uid)
    return allow is None or pid in allow


def lib_allowed(key: str) -> bool:
    if not enabled():
        return True
    key = str(key or "")
    if not key:
        return False
    libs = granted_libs(current().uid)
    if libs is None:
        return default_visible()
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
    allow = granted_sources(current().uid)
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
        if allow is not None and src not in allow:
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
        item = {"id": uid, "name": str(row.get("name") or uid),
                "source": "auto"}
        if row.get("is_admin"):
            item["is_admin"] = True
        out.append(item)
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


def is_admin(uid: str) -> bool:
    """该账号是否官方管理员；用于「旧播放记录只由管理员继承一次」等场景。"""
    uid = str(uid or "")
    if not uid:
        return False
    try:
        for row in discover().get("users") or []:
            if str(row.get("id") or "") == uid:
                return bool(row.get("is_admin"))
    except Exception:  # noqa: BLE001 - 判定失败按非管理员处理
        return False
    return False


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
    # 默认放行时「没配过」≠「不能用」，不该提示成缺口。
    unassigned_sources = ([] if default_online() else
                          [sid for sid in enabled_ids if sid not in granted_any])
    unassigned_subjects = ([] if (default_visible() and default_online()) else
                           [row["id"] for row in subjects_list
                            if not grants.get(row["id"])])
    return {
        "enabled": enabled(),
        "defaults": {"visible": default_visible(),
                     "online": default_online()},
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
            "sessions": len(report.get("sessions") or []),
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
            "sessions": [
                {"db": os.path.basename(str(s.get("db") or "")),
                 "table": s.get("table"), "token": (s.get("token_cols") or [""])[0],
                 "uid": s.get("uid_col")}
                for s in (report.get("sessions") or [])
            ],
            "error": report.get("error") or "",
        },
    }
