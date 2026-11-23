#!/usr/bin/env python3
"""hongguo-web 适配层：把一个自托管的红果短剧服务当成普通片源接入。

接口契约取自 https://github.com/kid941005/hongguo-web 的服务端源码
（server/http.mjs 路由 + server/catalog.mjs 的字段规范化）：

- ``GET /api/home?source=&page=``          → ``{classes:[{id,name}], items:[...]}``
- ``GET /api/category?source=&tid=&page=`` → ``{items:[...], hasMore}``
- ``GET /api/search?source=&q=&page=``     → ``{items:[...], hasMore, aggregate?}``
- ``GET /api/detail?source=&id=``          → ``{title,cover,desc,tags,
                                              lines:[{flag,name,episodes:[{id,name}]}]}``
- ``GET /api/stream/<epId>?q=auto``        → 服务端解密后的 MP4 流（支持 Range）

列表项字段：``{id,title,cover,remarks,tags[],year,area,desc,score}``。

播放：内置红果源的线路 ``flag`` 固定是 ``hg``，分集 id 直接喂 ``/api/stream``；
JS 源（flag 不是 hg）要先调 ``/api/play`` 换地址，本适配层暂不接管，详情里
会给出明确的「该线路暂不支持」提示，而不是塞一个播不出来的地址。
"""
from __future__ import annotations

import hashlib
import time
import urllib.parse

import httpx

import runtime

KIND = "hongguo"
# app.py 靠这个路径标记判断「无扩展名的直链媒体」，避免被当成 m3u8 去解析。
STREAM_MARK = "/api/stream/"
_UA = "fnmedia-ext"
_TIMEOUT = 8.0
_NATIVE_FLAG = "hg"


def enabled(source: dict | None) -> bool:
    return str((source or {}).get("kind") or "").strip().lower() == KIND


def base_url(source: dict | None) -> str:
    return str((source or {}).get("api") or "").strip().rstrip("/")


def new_id(url: str) -> str:
    """同一部署地址永远得到同一个源 id，重复录入会被识别成已存在。"""
    return "hg_" + hashlib.md5(str(url or "").encode()).hexdigest()[:10]


def _token(source: dict) -> str:
    return str((source or {}).get("token") or "").strip()


def _headers(source: dict) -> dict:
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    token = _token(source)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _params(source: dict, params: dict | None) -> dict:
    out = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    token = _token(source)
    if token:
        # 设置页的 TOKEN 既接受 Bearer 也接受 ?at=，媒体地址只能走 query。
        out["at"] = token
    return out


async def _get(client: httpx.AsyncClient, source: dict, path: str,
               params: dict | None = None,
               timeout: float = _TIMEOUT) -> dict | None:
    url = base_url(source) + path
    if not url.startswith(("http://", "https://")):
        return None
    try:
        resp = await client.get(url, params=_params(source, params),
                                headers=_headers(source), timeout=timeout)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:  # noqa: BLE001 - 服务不可用等价于没有数据
        return None
    return data if isinstance(data, dict) else None


async def probe(client: httpx.AsyncClient, source: dict) -> dict:
    """录入时校验地址：必须真的是 hongguo-web，而不是随便一个 200 的站点。"""
    url = base_url(source) + "/health"
    out = {"ok": False, "version": "", "active": "", "message": ""}
    if not url.startswith(("http://", "https://")):
        out["message"] = "地址必须以 http(s) 开头"
        return out
    try:
        resp = await client.get(url, headers=_headers(source),
                                timeout=min(_TIMEOUT, 6.0))
        data = resp.json() if resp.status_code == 200 else None
    except Exception as exc:  # noqa: BLE001 - 连不上就是不可用
        out["message"] = f"连接失败：{type(exc).__name__}"
        return out
    if not isinstance(data, dict) or str(data.get("service") or "") != "hongguo-web":
        out["message"] = "该地址不是 hongguo-web 服务"
        return out
    out["ok"] = bool(data.get("ok", True))
    out["version"] = str(data.get("version") or "")
    out["active"] = str(data.get("active") or "")
    if not out["ok"]:
        out["message"] = "服务自报不健康"
    return out


async def classes(client: httpx.AsyncClient, source: dict) -> list[dict]:
    """源站全部分类（``/api/home`` 的 classes；`tid` 即分类 id）。"""
    data = await _get(client, source, "/api/home", {"page": 1})
    out: list[dict] = []
    for row in (data or {}).get("classes") or []:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("id") or "").strip()
        if not cid:
            continue
        out.append({"id": cid, "name": str(row.get("name") or cid).strip()})
    return out


def titles(source: dict, rows) -> list:
    """hongguo 列表项 → 本项目 ``cms.OnlineTitle``。"""
    import cms

    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        vid = str(row.get("id") or "").strip()
        title = str(row.get("title") or "").strip()
        if not vid or not title:
            continue
        score = row.get("score")
        out.append(cms.OnlineTitle(
            provider=str(source.get("id") or ""),
            vod_id=vid,
            title=title,
            provider_name=str(source.get("name") or "红果"),
            # 红果全是短剧：列表阶段还没有集数，靠 type_name 让客户端按剧集展示。
            type_name="短剧",
            year=str(row.get("year") or ""),
            area=str(row.get("area") or ""),
            remarks=str(row.get("remarks") or ""),
            score=str(score) if score not in (None, "", 0) else "",
            poster=str(row.get("cover") or ""),
            overview=str(row.get("desc") or ""),
        ))
    return out


async def page_titles(client: httpx.AsyncClient, source: dict, tid: str,
                      page: int = 1) -> list:
    data = await _get(client, source, "/api/category",
                      {"tid": str(tid), "page": int(page)})
    return titles(source, (data or {}).get("items"))


async def search_titles(client: httpx.AsyncClient, source: dict,
                        keyword: str, page: int = 1) -> list:
    data = await _get(client, source, "/api/search",
                      {"q": keyword, "page": int(page)})
    return titles(source, (data or {}).get("items"))


def stream_url(source: dict, ep_id: str, quality: str = "auto") -> str:
    """内置红果分集流：服务端解密 + 支持 Range，可直接当 MP4 直链。"""
    token = _token(source)
    query = f"?q={urllib.parse.quote(quality or 'auto')}"
    if token:
        query += "&at=" + urllib.parse.quote(token, safe="")
    return (f"{base_url(source)}{STREAM_MARK}"
            f"{urllib.parse.quote(str(ep_id), safe='')}{query}")


def compat_url(url: str) -> str:
    """同一分集的 360P H.264 兜底地址（``q=compat``）。

    红果新版的高清档要走官方签名通道，被限流/熔断时 /api/stream 直接回
    503/空响应；compat 是明文 H.264 通道，通常仍可用。已经就是 compat、
    或者不是 /api/stream 地址时返回空串。
    """
    parts = urllib.parse.urlsplit(str(url or ""))
    if STREAM_MARK not in parts.path:
        return ""
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    if str((query.get("q") or ["auto"])[0]).strip().lower() == "compat":
        return ""
    query["q"] = ["compat"]
    return urllib.parse.urlunsplit((
        parts.scheme, parts.netloc, parts.path,
        urllib.parse.urlencode(query, doseq=True), parts.fragment))


def play_placeholder(source: dict, flag: str, ep_id: str) -> str:
    """非内置线路的占位标记；播放前会被识别并给出明确提示。"""
    return f"hgplay://{urllib.parse.quote(str(flag) or 'flag', safe='')}/" \
           f"{urllib.parse.quote(str(ep_id), safe='')}"


# --------------------------------------------------------------- 画质缓存
# 飞牛在起播前就会调 stream/list，而红果的真实档位只有播放响应头
# （x-stream-quality / x-stream-codec）里才有。这里记住每次实际播放命中的
# 档位，下一次 stream/list 直接回填，不额外探测、不增加起播延迟。
_QUALITY_TTL = 3600.0
_QUALITY_MAX = 500
_QUALITY: dict[tuple[str, str, str], tuple[float, str, str]] = {}
_QUALITY_DEPLOY: dict[tuple[str, str], tuple[float, str, str]] = {}
_QUALITY_SIZES = {
    "4k": (3840, 2160), "2160p": (3840, 2160),
    "2k": (2560, 1440), "1440p": (2560, 1440),
    "1080p": (1920, 1080), "720p": (1280, 720),
    "540p": (960, 540), "480p": (854, 480),
    "360p": (640, 360), "240p": (426, 240),
}
# 官网直连失败时服务端降级到 compat（360P H.264），头里就是 compat。
_QUALITY_ALIAS = {"compat": "360p"}


def _now() -> float:
    return time.monotonic()


def _deploy_parts(url: str) -> tuple[str, str, str] | None:
    """``/api/stream/<ep>`` → (部署域名, 路径前缀, 分集 id)。

    query 里的 token / 画质参数不参与，换 token 后同一集仍命中同一条缓存。
    """
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
    except ValueError:
        return None
    path = parsed.path or ""
    mark = path.rfind(STREAM_MARK)
    if mark < 0 or not parsed.netloc:
        return None
    ep = path[mark + len(STREAM_MARK):].strip("/")
    if not ep or "/" in ep:
        return None
    return (parsed.netloc.lower(), path[:mark].rstrip("/"), ep)


def episode_key(url: str) -> tuple[str, str, str] | None:
    return _deploy_parts(url)


def _quality_meta(quality: str, codec: str) -> dict | None:
    """响应头档位 → stream/list 可直接使用的分辨率 / 编码。"""
    label = str(quality or "").strip().lower()
    label = _QUALITY_ALIAS.get(label, label)
    if not label:
        return None
    size = _QUALITY_SIZES.get(label)
    if not size and label.endswith("p") and label[:-1].isdigit():
        height = int(label[:-1])
        size = (int(round(height * 16 / 9 / 2)) * 2, height)
    if not size:
        return None
    name = str(codec or "").strip().lower()
    if name in ("h265", "hevc"):
        name = "hevc"
    elif name not in ("h264", "av1"):
        name = "h264"
    return {"resolution": label, "width": size[0], "height": size[1],
            "codec": name}


def reset_quality_cache() -> None:
    """测试 / 手动排障用：清空已记住的画质。"""
    _QUALITY.clear()
    _QUALITY_DEPLOY.clear()


def _prune(now: float) -> None:
    for store in (_QUALITY, _QUALITY_DEPLOY):
        stale = [k for k, v in store.items() if now - v[0] > _QUALITY_TTL]
        for key in stale:
            store.pop(key, None)
    extra = len(_QUALITY) - _QUALITY_MAX
    if extra > 0:
        for key, _ in sorted(_QUALITY.items(), key=lambda kv: kv[1][0])[:extra]:
            _QUALITY.pop(key, None)


def remember_quality(url: str, quality: str, codec: str) -> None:
    """记录一次播放实际命中的档位；空头 / 非红果流直接忽略。"""
    parts = _deploy_parts(url)
    if not parts:
        return
    meta = _quality_meta(quality, codec)
    if not meta:
        return
    now = _now()
    entry = (now, meta["resolution"], meta["codec"])
    _QUALITY[parts] = entry
    _QUALITY_DEPLOY[parts[:2]] = entry
    _prune(now)


def _cached_meta(entry) -> dict | None:
    if not entry:
        return None
    ts, quality, codec = entry
    if _now() - ts > _QUALITY_TTL:
        return None
    return _quality_meta(quality, codec)


def cached_quality(url: str) -> dict | None:
    parts = _deploy_parts(url)
    if not parts:
        return None
    entry = _QUALITY.get(parts)
    if not entry:
        return None
    meta = _cached_meta(entry)
    if not meta:
        _QUALITY.pop(parts, None)
    return meta


def cached_quality_for_episode(brief: dict, index: int) -> dict | None:
    """按分集取已记住的画质；没有就退到同一部署最后一次已知档位。"""
    urls: list[str] = []
    for name in (brief or {}).get("line_names") or []:
        eps = ((brief or {}).get("lines") or {}).get(name) or []
        if not isinstance(eps, list) or not 0 <= index < len(eps):
            continue
        row = eps[index]
        url = row.get("url") if isinstance(row, dict) else ""
        if url:
            urls.append(str(url))
    for url in urls:
        meta = cached_quality(url)
        if meta:
            return meta
    # 这一集还没播放过（或分集缓存已过期）：同部署的其他分集档位相同，
    # 用它兜底，至少比写死 1080p 更接近真实画质。
    for url in urls:
        parts = _deploy_parts(url)
        if not parts:
            continue
        meta = _cached_meta(_QUALITY_DEPLOY.get(parts[:2]))
        if meta:
            return meta
    return None


def cached_quality_for_source(source: dict | None) -> dict | None:
    """stream/list 阶段 brief 可能还没有分集地址，用源地址找同部署缓存。"""
    try:
        parsed = urllib.parse.urlparse(base_url(source))
    except ValueError:
        return None
    if not parsed.netloc:
        return None
    key = (parsed.netloc.lower(), parsed.path.rstrip("/"))
    return _cached_meta(_QUALITY_DEPLOY.get(key))


def quality_hook(url: str):
    """给 ``hls.proxy_media`` 的响应头回调；非红果流返回 None。"""
    if not _deploy_parts(url):
        return None

    def hook(headers) -> None:
        try:
            lower = {str(k).lower(): v for k, v in dict(headers or {}).items()}
        except (TypeError, ValueError):
            return
        remember_quality(url, str(lower.get("x-stream-quality") or ""),
                         str(lower.get("x-stream-codec") or ""))

    return hook



def lines_of(source: dict, detail: dict) -> tuple[dict, bool]:
    """详情接口 → ``{线路名: [Episode]}``，返回 (lines, 是否原生线路)。"""
    import cms

    lines: dict[str, list] = {}
    native = True
    for line in (detail or {}).get("lines") or []:
        if not isinstance(line, dict):
            continue
        flag = str(line.get("flag") or "")
        name = str(line.get("name") or flag or f"线路{len(lines) + 1}").strip()
        eps = []
        for row in line.get("episodes") or []:
            if not isinstance(row, dict):
                continue
            eid = str(row.get("id") or "").strip()
            if not eid:
                continue
            ep_name = str(row.get("name") or f"第{len(eps) + 1}集").strip()
            if flag == _NATIVE_FLAG:
                url = stream_url(source, eid)
            else:
                url = play_placeholder(source, flag, eid)
            eps.append(cms.Episode(title=ep_name, url=url,
                                   direct=flag == _NATIVE_FLAG, headers={}))
        if eps:
            lines[name] = eps
            native = native and flag == _NATIVE_FLAG
    return lines, native


async def detail(client: httpx.AsyncClient, source: dict, vid: str) -> dict | None:
    data = await _get(client, source, "/api/detail", {"id": str(vid)})
    return data if isinstance(data, dict) else None


def find_source(sid: str) -> dict | None:
    import config

    for source in config.all_sources():
        if str(source.get("id") or "") == str(sid or ""):
            return source
    return None


async def ensure_lines(client: httpx.AsyncClient, store, brief: dict) -> dict:
    """给还没有线路的 hongguo 条目补一次详情（搜索结果本身不带播放地址）。"""
    if not isinstance(brief, dict) or brief.get("lines"):
        return brief
    source = find_source(str(brief.get("provider") or ""))
    if not source or not enabled(source):
        return brief
    data = await detail(client, source, brief.get("vod_id"))
    if not data:
        return brief
    lines, native = lines_of(source, data)
    if not lines:
        return brief
    updated = dict(brief)
    # brief 里的线路是「纯 dict」结构（fake.brief_of 的契约），不是 Episode 对象；
    # 直接塞 dataclass 会让播放链的 _pick 在 .get() 上崩掉。
    updated["lines"] = {
        name: [{"title": ep.title, "url": ep.url, "direct": ep.direct,
                "headers": ep.headers} for ep in eps]
        for name, eps in lines.items()
    }
    updated["line_names"] = list(lines)
    updated["line_sources"] = {name: str(source.get("id") or "") for name in lines}
    updated["episodes"] = max(len(eps) for eps in lines.values())
    updated["is_tv"] = True
    updated["primary_ext"] = "mp4" if native else "m3u8"
    for key, value in (("overview", data.get("desc")),
                       ("poster", data.get("cover")),
                       ("remarks", data.get("remarks"))):
        if value and not updated.get(key):
            updated[key] = str(value)
    try:
        store.put_brief(updated)
    except Exception as exc:  # noqa: BLE001 - 注册失败仍返回补好的 brief
        runtime.log(f"hongguo put_brief failed: {type(exc).__name__}")
    runtime.log(f"hongguo detail: {str(updated.get('title'))[:24]} "
                f"lines={list(lines)[:2]} eps={updated.get('episodes')}")
    return updated
