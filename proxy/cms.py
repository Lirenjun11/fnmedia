#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import re
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace

import httpx

_MEDIA = re.compile(r"\.(m3u8|mp4|flv|mkv|ts)(\?|$)", re.I)
_UA = "Mozilla/5.0 (compatible; fnmedia-ext/0.1)"
_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")


def clean_text(value: str) -> str:
    text = html_lib.unescape(str(value or "")).replace("\xa0", " ")
    text = re.sub(r"(?i)<\s*(br|/p|/div|/li)\s*/?>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    lines = [_WS_RE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


@dataclass
class Episode:
    title: str
    url: str
    direct: bool = True
    headers: dict = field(default_factory=dict)


@dataclass
class OnlineTitle:
    provider: str
    vod_id: str
    title: str
    provider_name: str = ""
    type_name: str = ""
    year: str = ""
    area: str = ""
    remarks: str = ""
    score: str = ""
    poster: str = ""
    overview: str = ""
    actor: str = ""
    director: str = ""
    updated: str = ""
    lines: dict = field(default_factory=dict)

    @property
    def online_id(self) -> str:
        return f"{self.provider}:{self.vod_id}"


_TV_WORDS = ("电视剧", "连续剧", "日剧", "韩剧", "美剧", "英剧", "泰剧", "国剧",
             "日本剧", "韩国剧", "欧美剧", "海外剧", "港台剧", "动漫", "番剧",
             "动画剧", "综艺")
_MOVIE_WORDS = ("电影", "影片", "剧情片", "动作片", "喜剧片", "爱情片", "科幻片",
                "恐怖片", "纪录片")


def is_tv(t: OnlineTitle) -> bool:
    typ = t.type_name or ""
    if any(w in typ for w in _MOVIE_WORDS):
        return False
    return episode_count(t) > 1 or any(w in typ for w in _TV_WORDS)


def line_names(t: OnlineTitle) -> list[str]:
    return [name for name, eps in t.lines.items() if eps]


def episode_count(t: OnlineTitle) -> int:
    names = line_names(t)
    return max((len(t.lines[n]) for n in names), default=0)


def parse_play(play_from: str, play_url: str, api: str) -> dict:
    parts = (play_url or "").split("$$$")
    froms = (play_from or "").split("$$$")
    parsed = urllib.parse.urlparse(api)
    referer = f"{parsed.scheme}://{parsed.netloc}/"
    out: dict[str, list[Episode]] = {}
    for i, group in enumerate(parts):
        name = (froms[i] if i < len(froms) and froms[i] else f"线路{i + 1}").strip()
        eps = []
        for raw in group.split("#"):
            if "$" not in raw:
                continue
            ep_name, url = raw.split("$", 1)
            url = url.strip()
            if not url.startswith(("http://", "https://")):
                continue
            eps.append(Episode(title=ep_name.strip() or f"第{len(eps) + 1}集",
                               url=url, direct=bool(_MEDIA.search(url)),
                               headers={"Referer": referer, "User-Agent": _UA}))
        if eps:
            out[name] = eps
    return out


def _xml_to_json(text: str) -> dict | None:
    if not text.lstrip().startswith("<"):
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    node = root.find(".//list")
    if node is None:
        return {"code": 1, "list": []}

    def txt(el, name):
        child = el.find(name)
        return (child.text or "").strip() if child is not None else ""

    rows = []
    for video in node.findall("video"):
        froms, groups = [], []
        for dd in video.findall(".//dl/dd"):
            flag = (dd.get("flag") or "").strip() or f"线路{len(froms) + 1}"
            play = (dd.text or "").strip()
            if play:
                froms.append(flag)
                groups.append(play)
        rows.append({
            "vod_id": txt(video, "id"), "vod_name": txt(video, "name"),
            "type_name": txt(video, "type"), "vod_year": txt(video, "year"),
            "vod_area": txt(video, "lang") or txt(video, "area"),
            "vod_remarks": txt(video, "note"), "vod_score": txt(video, "score"),
            "vod_pic": txt(video, "pic"), "vod_content": txt(video, "des"),
            "vod_actor": txt(video, "actor"), "vod_director": txt(video, "director"),
            "vod_play_from": "$$$".join(froms), "vod_play_url": "$$$".join(groups),
        })
    return {"code": 1, "list": rows}


def map_vod(v: dict, source: dict) -> OnlineTitle | None:
    title = str(v.get("vod_name") or "").strip()
    if not title or not v.get("vod_id"):
        return None
    lines = parse_play(str(v.get("vod_play_from") or ""),
                       str(v.get("vod_play_url") or ""), source["api"])
    if not lines:
        return None
    poster = str(v.get("vod_pic") or "").strip()
    if poster and not poster.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(source["api"])
        poster = urllib.parse.urljoin(
            f"{parsed.scheme}://{parsed.netloc}/", poster.lstrip("/"))
    return OnlineTitle(
        provider=source["id"], vod_id=str(v["vod_id"]), title=title,
        provider_name=str(source.get("name") or source.get("id") or ""),
        type_name=clean_text(v.get("type_name") or ""),
        year=str(v.get("vod_year") or ""),
        area=str(v.get("vod_area") or ""), remarks=str(v.get("vod_remarks") or ""),
        score=str(v.get("vod_score") or ""), poster=poster,
        overview=clean_text(v.get("vod_content") or v.get("vod_blurb") or ""),
        actor=str(v.get("vod_actor") or ""), director=str(v.get("vod_director") or ""),
        updated=str(v.get("vod_time") or v.get("vod_time_add") or ""),
        lines=lines,
    )


_NORM_RE = re.compile(r"[\s·・:：,，。.．\-—_（）()\[\]【】'\"!！?？~～]+")


def _norm_title(text) -> str:
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    return _NORM_RE.sub("", s)


def _year4(t: OnlineTitle) -> str:
    value = str(t.year or "")[:4]
    return value if value.isdigit() else ""


def _merge_bucket(bucket: list[OnlineTitle]) -> list[OnlineTitle]:
    first_by_provider: dict[str, OnlineTitle] = {}
    extra: list[OnlineTitle] = []
    for t in bucket:
        if t.provider in first_by_provider:
            extra.append(t)
        else:
            first_by_provider[t.provider] = t
    if len(first_by_provider) <= 1:
        return bucket
    ordered = sorted(first_by_provider.values(), key=lambda x: x.provider)
    primary = ordered[0]
    lines: dict[str, list] = {}
    for t in ordered:
        prefix = str(t.provider_name or t.provider).strip()
        for name, eps in t.lines.items():
            key = name if t is primary else f"{prefix}：{name}"
            n = 2
            while key in lines:
                key = f"{prefix}：{name}·{n}"
                n += 1
            lines[key] = eps
    return [replace(primary, lines=lines)] + extra


def merge_titles(titles: list[OnlineTitle]) -> list[OnlineTitle]:
    """同名影片跨源合并为一张卡，线路名带上来源前缀。"""
    groups: dict[str, list[OnlineTitle]] = {}
    for t in titles:
        groups.setdefault(_norm_title(t.title), []).append(t)
    out: list[OnlineTitle] = []
    for group in groups.values():
        by_year: dict[str, list[OnlineTitle]] = {}
        for t in group:
            by_year.setdefault(_year4(t), []).append(t)
        years = [year for year in by_year if year]
        if len(years) > 1:
            buckets = list(by_year.values())
        elif len(years) == 1 and "" in by_year:
            buckets = [by_year[years[0]] + by_year[""]]
        else:
            buckets = [group]
        for bucket in buckets:
            out.extend(_merge_bucket(bucket))
    return out


async def search_source(client: httpx.AsyncClient, source: dict,
                        keyword: str, timeout: float) -> list[OnlineTitle]:
    try:
        resp = await client.get(source["api"], params={"ac": "detail", "wd": keyword},
                                headers={"User-Agent": _UA}, timeout=timeout)
        if resp.status_code != 200:
            return []
        data = _xml_to_json(resp.text) if "/at/xml" in source["api"].lower() else resp.json()
    except Exception:  
        return []
    if not isinstance(data, dict) or str(data.get("code")) != "1":
        return []
    out = []
    for row in data.get("list") or []:
        try:
            item = map_vod(row, source)
        except Exception:  
            continue
        if item:
            out.append(item)
    return out


_CACHE: dict[str, tuple[float, list[OnlineTitle]]] = {}
_INFLIGHT: dict[str, asyncio.Task] = {}
_CACHE_TTL = 120.0


async def search_all(client: httpx.AsyncClient, sources: list[dict], keyword: str,
                     timeout: float = 2.5, concurrency: int = 5,
                     enough: int = 24) -> list[OnlineTitle]:
    keyword = (keyword or "").strip()
    if not keyword or not sources:
        return []
    hit = _CACHE.get(keyword)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL:
        return hit[1]
    task = _INFLIGHT.get(keyword)
    if task and not task.done():
        return await task

    async def run() -> list[OnlineTitle]:
        sem = asyncio.Semaphore(max(1, concurrency))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.5, timeout)

        async def one(src):
            async with sem:
                return await search_source(client, src, keyword, timeout=timeout)

        tasks = [asyncio.create_task(one(s)) for s in sources]
        found: list[OnlineTitle] = []
        try:
            for fut in asyncio.as_completed(tasks):
                remain = deadline - loop.time()
                if remain <= 0:
                    break
                try:
                    found.extend(await asyncio.wait_for(fut, timeout=remain))
                except Exception:  
                    break
                if len(found) >= enough:
                    break
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
        dedup, seen = [], set()
        for item in found:
            key = (item.provider, item.vod_id)
            if key not in seen:
                seen.add(key)
                dedup.append(item)
        if len(_CACHE) > 200:
            _CACHE.clear()
        _CACHE[keyword] = (time.monotonic(), dedup)
        return dedup

    task = asyncio.create_task(run())
    _INFLIGHT[keyword] = task
    try:
        return await task
    finally:
        _INFLIGHT.pop(keyword, None)


def clear_cache() -> int:
    n = len(_CACHE)
    _CACHE.clear()
    return n


class TitleStore:

    def __init__(self, max_items: int = 2000, ttl: float = 86400.0):
        self._items: dict[str, dict] = {}
        self._media: dict[str, tuple[str, int]] = {}
        self._episode: dict[str, tuple[str, int]] = {}
        self._season: dict[str, str] = {}
        self._max = max_items
        self._ttl = ttl

    @staticmethod
    def guid_of(t: OnlineTitle) -> str:
        return hashlib.md5(f"on_{t.provider}_{t.vod_id}".encode()).hexdigest()

    def remember(self, titles: list[OnlineTitle]) -> list[dict]:
        out = []
        from fake import (brief_of, media_guid, video_guid, ep_media_guid,
                          ep_video_guid, episode_guid, season_guid)
        for t in titles:
            guid = self.guid_of(t)
            brief = brief_of(t, guid)
            self._items[guid] = brief
            self._season[season_guid(guid)] = guid
            self._media[media_guid(guid)] = (guid, 0)
            self._media[video_guid(guid)] = (guid, 0)
            for idx in range(max(1, episode_count(t))):
                eg = episode_guid(guid, idx)
                self._episode[eg] = (guid, idx)
                self._media[ep_media_guid(eg)] = (guid, idx)
                self._media[ep_video_guid(eg)] = (guid, idx)
            out.append(brief)
        if len(self._items) > self._max:
            for key in list(self._items)[: max(1, self._max // 4)]:
                self._drop(key)
        return out

    def get(self, guid: str) -> dict | None:
        return self._items.get(guid)

    def resolve_media(self, mg: str) -> tuple[str, int] | None:
        return self._media.get(mg)

    def resolve_episode(self, eg: str) -> tuple[str, int] | None:
        return self._episode.get(eg)

    def resolve_season(self, sg: str) -> str | None:
        return self._season.get(sg)

    def count(self) -> int:
        return len(self._items)

    def _drop(self, guid: str) -> None:
        brief = self._items.pop(guid, None)
        if brief is None:
            return
        from fake import (media_guid, video_guid, ep_media_guid, ep_video_guid,
                          episode_guid, season_guid)
        self._season.pop(season_guid(guid), None)
        self._media.pop(media_guid(guid), None)
        self._media.pop(video_guid(guid), None)
        for idx in range(max(1, int(brief.get("episodes") or 1))):
            eg = episode_guid(guid, idx)
            self._episode.pop(eg, None)
            self._media.pop(ep_media_guid(eg), None)
            self._media.pop(ep_video_guid(eg), None)
