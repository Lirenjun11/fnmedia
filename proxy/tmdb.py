#!/usr/bin/env python3
"""TMDB 元数据富化（可选，按需 + 缓存，绝不阻塞搜索与起播）。

按「规范片名 + 年份」匹配，取中文简介 / 类型 / 评分 / 时长 / 横版背景 /
原名与演员表。只读 TMDB 官方 API（api.themoviedb.org）与官方图床
（image.tmdb.org），API Key 只存本机 config，不入包、不上传。

调用约定：
- ``cached(brief)``      纯缓存读取，零网络，可在任何同步代码里调用；
- ``await enrich(brief)`` 详情/历史接口用：命中立即返回，未命中在后台富化并
  最多等 ``_BUDGET`` 秒，超时先返回源站数据，后台补完再写缓存；
- ``prefetch(...)``      只触发后台任务，不等待。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata

import httpx

import config

_API = "https://api.themoviedb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p"
_CACHE_FILE = config.STATE_DIR / "tmdb_cache.json"

_TTL = 7 * 86400.0        # 命中结果保留 7 天
_NEG_TTL = 15 * 60.0      # 未命中/瞬时失败只负缓存 15 分钟，避免锁死恢复
_BUDGET = 1.0             # 详情接口最多为富化等待的秒数
_MAX_CACHE = 2000
_MAX_INFLIGHT = 6         # 同时最多 6 个后台富化任务，避免打爆 TMDB
_GOOD_TTL = 300.0

_CACHE: dict[str, dict] = {}
_INFLIGHT: dict[str, asyncio.Task] = {}
_LOADED = False
_GOOD: dict = {"channel": "", "until": 0.0}
_CFG: dict = {}
_CFG_TS = 0.0
_CFG_TTL = 2.0

_TV_WORDS = ("电视剧", "连续剧", "剧集", "综艺", "真人秀", "动漫", "番剧",
             "动画剧", "日韩剧", "欧美剧", "国产剧", "大陆剧", "港台剧",
             "韩剧", "日剧", "美剧", "英剧", "泰剧")


# ------------------------------------------------------------------ 配置
def _cfg(force: bool = False) -> dict:
    """配置快照（2 秒缓存）。``config.get()`` 会 deepcopy 整个配置，
    历史列表逐条调用时不能每次都付这个代价。"""
    global _CFG, _CFG_TS
    now = time.monotonic()
    if force or not _CFG or now - _CFG_TS > _CFG_TTL:
        try:
            _CFG = config.get()
        except Exception:
            _CFG = {}
        _CFG_TS = now
    return _CFG


def api_key() -> str:
    return str(_cfg().get("tmdb_api_key") or "").strip()


def enabled() -> bool:
    """总开关 + Key 都存在才真正发请求；缺 Key 时全链路短路。"""
    return bool(_cfg().get("tmdb_enabled", True)) and bool(api_key())


# ------------------------------------------------------------------ 片名
_PUNCT_RE = re.compile(r"[\s\u3000:：·・\-—_|,，.。!！?？'\"()（）\[\]【】]+")


def _norm(text) -> str:
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    return _PUNCT_RE.sub("", s)


def clean_title(title) -> str:
    """去掉资源站展示后缀，保留 TMDB 检索需要的真实片名。"""
    t = re.sub(r"【[^】]*】", "", str(title or "")).strip()
    t = re.split(r"\s*[·|｜]\s*(?:正片|已完结|HD.*|BD.*|高清.*|中字.*)$",
                 t, maxsplit=1)[0].strip()
    t = re.sub(r"(?:普通话版|国语版|粤语版|英语版|原声版|中字版|高清版|完整版)$",
               "", t).strip()
    t = re.sub(r"[（(](?:19|20)\d{2}[)）]\s*$", "", t).strip()
    return t or str(title or "").strip()


def is_tv_brief(brief: dict) -> bool:
    """判断该走 /search/tv 还是 /search/movie。

    优先看分类名：源站的「动漫/综艺/国产剧」等分类比 is_tv 可靠（后者对
    单集连载剧、综艺的推断会漂移）；「动画电影」这类含「电影」的先判电影。
    """
    typ = str(brief.get("type_name") or "")
    # 「动作片 / 科幻片 / 纪录片 / 动画电影」都是电影；先判电影再判剧集
    if "电影" in typ or "剧情片" in typ or typ.endswith("片"):
        return False
    if any(word in typ for word in _TV_WORDS) or typ.endswith("剧"):
        return True
    return bool(brief.get("is_tv"))


def _cache_key(title: str, year: str, is_tv: bool) -> str:
    return f"{'tv' if is_tv else 'movie'}:{_norm(title)}:{year or ''}"


def _year(brief: dict) -> str:
    m = re.search(r"(19|20)\d{2}", str(brief.get("year") or ""))
    return m.group(0) if m else ""


# ------------------------------------------------------------------ 缓存
def _load() -> None:
    global _LOADED, _CACHE
    if _LOADED:
        return
    _LOADED = True
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict):
        _CACHE = {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _prune() -> None:
    if len(_CACHE) <= _MAX_CACHE:
        return
    for old in sorted(_CACHE, key=lambda k: float(_CACHE[k].get("ts") or 0)
                      )[:len(_CACHE) - _MAX_CACHE + 500]:
        _CACHE.pop(old, None)


def _save() -> None:
    _prune()
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_CACHE, ensure_ascii=False,
                                  separators=(",", ":")), encoding="utf-8")
        tmp.replace(_CACHE_FILE)
    except OSError:
        pass


def _read(key: str) -> tuple[dict | None, bool]:
    """返回 (数据, 是否新鲜)。负缓存新鲜时返回 (None, True)，调用方不再等。"""
    _load()
    entry = _CACHE.get(key)
    if not isinstance(entry, dict):
        return None, False
    age = time.time() - float(entry.get("ts") or 0)
    data = entry.get("data")
    if data is None:
        return None, age < _NEG_TTL
    if age > _TTL:
        return None, False
    return data, True


def _write(key: str, data: dict | None) -> None:
    _load()
    _CACHE[key] = {"ts": time.time(), "data": data}
    _save()


# ------------------------------------------------------------------ 网络
def _channels() -> list[tuple[str, str]]:
    """可用的出口通道：配置的 TMDB 专用代理 → 资源站代理 → 直连。

    上次成功的通道排在最前，避免每次详情都先等直连超时。
    """
    c = _cfg()
    out: list[tuple[str, str]] = []
    for label, field in (("tmdb_proxy", "tmdb_proxy"),
                         ("source_proxy", "source_proxy")):
        value = str(c.get(field) or "").strip()
        if value and value not in [proxy for _, proxy in out]:
            out.append((label, value))
    good = str(_GOOD.get("channel") or "")
    if good and time.time() < float(_GOOD.get("until") or 0):
        out.sort(key=lambda row: 0 if row[1] == good else 1)
    out.append(("direct", ""))
    return out


def _mark_good(proxy: str) -> None:
    _GOOD.update(channel=proxy, until=time.time() + _GOOD_TTL)


def _client(proxy: str) -> httpx.AsyncClient:
    opts = dict(timeout=httpx.Timeout(12.0, connect=6.0),
                follow_redirects=True, trust_env=False,
                headers={"Accept": "application/json"})
    if proxy:
        return httpx.AsyncClient(proxy=proxy, **opts)
    return httpx.AsyncClient(**opts)


class _AuthError(Exception):
    """API Key 被拒：换出口通道没有意义，直接结束。"""


async def _get(client: httpx.AsyncClient, path: str, params: dict) -> dict | None:
    query = dict(params)
    query["api_key"] = api_key()
    query.setdefault("language", "zh-CN")
    try:
        resp = await client.get(_API + path, params=query)
    except Exception:
        return None
    if resp.status_code in (401, 403):
        raise _AuthError(str(resp.status_code))
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _year_of(row: dict, is_tv: bool) -> str:
    date = row.get("first_air_date" if is_tv else "release_date") or ""
    return str(date)[:4]


def _pick(results: list, title: str, year: str, is_tv: bool) -> dict | None:
    """标题归一化相等 + 年份相符优先，其次热度；完全无关的高热度片拒绝。"""
    rows = [row for row in results or [] if isinstance(row, dict)]
    if not rows:
        return None
    wanted = _norm(title)

    def score(row: dict) -> float:
        name = row.get("name" if is_tv else "title") or ""
        orig = row.get("original_name" if is_tv else "original_title") or ""
        by_year = _year_of(row, is_tv)
        value = 0.0
        if _norm(name) == wanted or _norm(orig) == wanted:
            value += 100
        if year and by_year == year:
            value += 50
        elif year and by_year and by_year.isdigit() \
                and abs(int(by_year) - int(year)) <= 1:
            value += 20
        value += min(float(row.get("popularity") or 0), 50) / 10
        return value

    best = max(rows, key=score)
    name = _norm(best.get("name" if is_tv else "title") or "")
    orig = _norm(best.get("original_name" if is_tv else "original_title") or "")
    title_match = bool(wanted and (
        wanted in name or name in wanted
        or (orig and (wanted in orig or orig in wanted))))
    # 只靠年份命中就采用会套错完全无关的片（详情页显示成另一部电影）；
    # 片名必须有交集，年份只用于在多个同名结果里排序。
    return best if title_match else None


def img(path: str, size: str = "w500") -> str:
    if not path:
        return ""
    return f"{IMG_BASE}/{size}{path}"


def _first(values) -> str:
    for value in values or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return 0


async def _fetch_meta(title: str, year: str, is_tv: bool) -> dict | None:
    kind = "tv" if is_tv else "movie"
    for _label, proxy in _channels():
        client = _client(proxy)
        try:
            found = await _get(client, f"/search/{kind}", {"query": title})
            if not found:
                continue
            best = _pick(found.get("results") or [], title, year, is_tv)
            _mark_good(proxy)
            if not best:
                return None
            tid = best.get("id")
            detail, credits = await asyncio.gather(
                _get(client, f"/{kind}/{tid}", {}),
                _get(client, f"/{kind}/{tid}/credits", {}),
                return_exceptions=True)
            if isinstance(detail, BaseException):
                detail = None
            if isinstance(credits, BaseException):
                credits = None
            detail = detail or {}
            credits = credits or {}
            cast = []
            for person in (credits.get("cast") or [])[:12]:
                if not isinstance(person, dict):
                    continue
                cast.append({
                    "id": person.get("id"),
                    "name": person.get("name") or "",
                    "original_name": (person.get("original_name")
                                      or person.get("name") or ""),
                    "character": person.get("character") or "",
                    "profile": img(person.get("profile_path"), "w185"),
                })
            crew = [row for row in (credits.get("crew") or [])
                    if isinstance(row, dict)]
            director = next((row.get("name") or "" for row in crew
                             if row.get("job") == "Director"), "")
            genres = [g.get("name") or "" for g in (detail.get("genres") or [])
                      if isinstance(g, dict) and g.get("name")]
            poster = detail.get("poster_path") or best.get("poster_path") or ""
            backdrop = (detail.get("backdrop_path")
                        or best.get("backdrop_path") or "")
            runtime = _first([detail.get("runtime")])
            if is_tv and not runtime:
                runtime = _first(detail.get("episode_run_time"))
            air = detail.get("first_air_date") or ""
            return {
                "tmdb_id": tid,
                "tmdb_type": kind,
                "title": detail.get("name" if is_tv else "title") or title,
                "original_title": (detail.get("original_name" if is_tv
                                              else "original_title") or ""),
                "overview": detail.get("overview") or best.get("overview") or "",
                "vote_average": round(float(detail.get("vote_average")
                                            or best.get("vote_average") or 0), 1),
                "genres": genres,
                "runtime": runtime,
                "release_date": (detail.get("release_date") or air
                                 or best.get("release_date") or ""),
                "poster": img(poster, "w500"),
                "backdrop": img(backdrop, "w1280"),
                "cast": cast,
                "director": director,
                "number_of_seasons": detail.get("number_of_seasons") or 0,
                "number_of_episodes": detail.get("number_of_episodes") or 0,
            }
        except _AuthError:
            return None
        except Exception:
            continue
        finally:
            try:
                await client.aclose()
            except Exception:
                pass
    return None


# ------------------------------------------------------------------ 对外
async def _work(key: str, title: str, year: str, is_tv: bool) -> dict | None:
    data = None
    try:
        data = await _fetch_meta(title, year, is_tv)
    except Exception:
        data = None
    finally:
        try:
            _write(key, data)
        finally:
            _INFLIGHT.pop(key, None)
    return data


def _spawn(key: str, title: str, year: str, is_tv: bool):
    task = _INFLIGHT.get(key)
    if task is not None and not task.done():
        return task
    if len(_INFLIGHT) >= _MAX_INFLIGHT:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(_work(key, title, year, is_tv))
    _INFLIGHT[key] = task
    return task


def prefetch(title: str, year: str = "", is_tv: bool = False) -> None:
    if not enabled() or not title:
        return
    key = _cache_key(title, year, is_tv)
    _data, fresh = _read(key)
    if fresh:
        return
    _spawn(key, title, year, is_tv)


def prefetch_brief(brief: dict) -> None:
    if not isinstance(brief, dict):
        return
    prefetch(clean_title(brief.get("title")), _year(brief),
             is_tv_brief(brief))


def cached(brief: dict) -> dict | None:
    """纯缓存读取（零网络），没有新鲜命中返回 None。"""
    if not enabled() or not isinstance(brief, dict):
        return None
    title = clean_title(brief.get("title"))
    if not title:
        return None
    data, fresh = _read(_cache_key(title, _year(brief), is_tv_brief(brief)))
    return data if fresh else None


async def enrich(brief: dict, budget: float = _BUDGET) -> dict | None:
    """详情/历史用：缓存命中立即返回；未命中后台富化并最多等 ``budget`` 秒。"""
    if not enabled() or not isinstance(brief, dict):
        return None
    title = clean_title(brief.get("title"))
    if not title:
        return None
    year = _year(brief)
    is_tv = is_tv_brief(brief)
    key = _cache_key(title, year, is_tv)
    data, fresh = _read(key)
    if fresh:
        return data
    task = _spawn(key, title, year, is_tv)
    if task is None:
        return data
    try:
        return await asyncio.wait_for(asyncio.shield(task), budget)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return data
    except Exception:
        return data


async def probe() -> dict:
    """管理页「测试连接」：验证 Key 与出口通道，返回可读结果。"""
    _cfg(force=True)  # 刚保存的 Key / 代理要立刻生效，不吃 2 秒快照
    if not enabled():
        if not api_key():
            return {"ok": False, "error": "未配置 API Key"}
        return {"ok": False, "error": "TMDB 开关已关闭"}
    started = time.time()
    last = "网络不可达"
    for label, proxy in _channels():
        client = _client(proxy)
        try:
            resp = await client.get(_API + "/configuration",
                                    params={"api_key": api_key()})
        except Exception as exc:
            last = f"{label}: {type(exc).__name__}"
            continue
        finally:
            try:
                await client.aclose()
            except Exception:
                pass
        if resp.status_code == 200:
            _mark_good(proxy)
            return {"ok": True, "channel": label,
                    "latency_ms": int((time.time() - started) * 1000)}
        if resp.status_code in (401, 403):
            return {"ok": False, "error": "API Key 无效或未授权（HTTP %d）"
                                          % resp.status_code}
        last = f"{label}: HTTP {resp.status_code}"
    return {"ok": False, "error": last}


def status() -> dict:
    _load()
    hits = sum(1 for entry in _CACHE.values() if entry.get("data") is not None)
    return {
        "enabled": enabled(),
        "has_key": bool(api_key()),
        "cached": hits,
        "channel": str(_GOOD.get("channel") or ""),
        "inflight": len(_INFLIGHT),
    }
