#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import re
from datetime import date, datetime

import cms

TAG = "【在线】"

TV_CARD_KEEP = (
    "guid", "video_guid", "lan", "imdb_id", "trim_id", "tv_title",
    "parent_guid", "parent_title", "title", "type", "poster",
    "is_favorite", "watched", "watched_ts", "vote_average", "media_stream",
    "release_date", "season_number", "episode_number", "air_date",
    "number_of_seasons", "number_of_episodes",
    "local_number_of_seasons", "local_number_of_episodes", "status",
    "overview", "ancestor_guid", "ancestor_name", "ancestor_category",
    "ts", "duration", "single_child_guid", "file_name", "path", "runtime",
    "media_guid", "audio_guid", "subtitle_guid", "is_watched", "logos",
    "can_play", "play_item_guid", "play_error", "posters", "backdrops",
)


def h(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def media_guid(guid: str) -> str:
    return h("media:" + guid)


def video_guid(guid: str) -> str:
    return h("video:" + guid)


def season_guid(guid: str, season: int = 1) -> str:
    return h(f"season{season}:{guid}")


def episode_guid(guid: str, index: int) -> str:
    return h(f"ep:{guid}:{index}")


def ep_media_guid(eg: str) -> str:
    return h("media:" + eg)


def ep_video_guid(eg: str) -> str:
    return h("video:" + eg)


def poster_path(url: str) -> str:
    if not url or not url.startswith("http"):
        return ""
    token = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"/online/{token}.jpg"


def is_tv_client(ua: str) -> bool:
    low = (ua or "").lower()
    return "com.trim.tv" in low or "trim.tv" in low


def is_browser_client(ua: str) -> bool:
    low = (ua or "").lower()
    if any(m in low for m in ("com.trim", "fnos", "okhttp", "cfnetwork",
                              "dart", "exoplayer", "ijkplayer", "mediaplayer")):
        return False
    return any(m in low for m in ("mozilla", "applewebkit", "safari",
                                  "chrome", "firefox", "edg/"))


def _date(v) -> str:
    s = str(v if v is not None else "").strip()
    if not s or s.lower() in ("0", "none", "null", "-", "nan"):
        return ""
    m = re.fullmatch(r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?", s)
    try:
        if m:
            return date(int(m[1]), int(m[2] or 1), int(m[3] or 1)).isoformat()
        if re.match(r"^\d{4}-\d{2}-\d{2}[T ]", s):
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except (TypeError, ValueError):
        return ""
    return ""


def normalize_card(row: dict, ua: str) -> dict:
    if not is_tv_client(ua) or not isinstance(row, dict):
        return row
    out = {k: row[k] for k in TV_CARD_KEEP if k in row}
    out["status"] = "1"
    out["release_date"] = _date(row.get("release_date", ""))
    out["air_date"] = _date(row.get("air_date", ""))
    out.setdefault("path", "")
    out.setdefault("runtime", 0)
    if not out.get("video_guid") and out.get("guid"):
        out["video_guid"] = video_guid(out["guid"])
    return out


def normalize_cards(rows: list, ua: str) -> list:
    return [normalize_card(r, ua) for r in rows] if is_tv_client(ua) else rows


def normalize_detail(obj, ua: str):
    if not is_tv_client(ua):
        return obj

    def walk(o):
        if isinstance(o, dict):
            return {k: (_date(v) if k in ("release_date", "air_date")
                        else "1" if k == "status" else walk(v))
                    for k, v in o.items()}
        if isinstance(o, list):
            return [walk(x) for x in o]
        return o

    return walk(obj)


def brief_of(t: cms.OnlineTitle, guid: str) -> dict:
    lines = {
        name: [{"title": e.title, "url": e.url, "direct": e.direct,
                "headers": e.headers} for e in eps]
        for name, eps in t.lines.items() if eps
    }
    names = cms.line_names(t)
    first = (t.lines.get(names[0]) or []) if names else []
    first_url = first[0].url if first else ""
    return {
        "guid": guid, "provider": t.provider,
        "provider_name": t.provider_name or t.provider, "vod_id": t.vod_id,
        "title": t.title, "type_name": t.type_name, "year": t.year,
        "area": t.area, "remarks": t.remarks, "score": t.score,
        "poster": t.poster, "overview": t.overview, "actor": t.actor,
        "director": t.director, "updated": t.updated, "lines": lines,
        "is_tv": cms.is_tv(t), "episodes": cms.episode_count(t),
        "line_names": names,
        "primary_ext": "mp4" if ".mp4" in first_url.lower() else "m3u8",
    }


def search_card(b: dict) -> dict:
    return {
        "guid": b["guid"], "video_guid": video_guid(b["guid"]),
        "path": "", "runtime": 0, "lan": "zh-CN", "imdb_id": "", "trim_id": "",
        "tv_title": "", "parent_guid": "", "parent_title": "",
        "title": TAG + b["title"] + (f" ·{b['remarks']}" if b.get("remarks") else ""),
        "type": "TV" if b.get("is_tv") else "Movie",
        "genres": [], "poster": poster_path(b.get("poster", "")),
        "poster_width": 0, "poster_height": 0, "is_favorite": 0,
        "watched": 0, "watched_ts": 0,
        "vote_average": str(b.get("score") or "0"),
        "media_stream": {"resolutions": None, "audio_type": None,
                         "color_range_type": None},
        "release_date": b.get("year") or "", "season_number": 0,
        "episode_number": 0, "air_date": b.get("year") or "",
        "number_of_seasons": 1 if b.get("is_tv") else 0,
        "number_of_episodes": b.get("episodes", 0),
        "local_number_of_seasons": 0, "local_number_of_episodes": 0,
        "status": "在线", "overview": cms.clean_text(b.get("overview") or "")[:1000],
        "ancestor_guid": "", "ancestor_name": "🌐在线资源",
        "ancestor_category": "TV" if b.get("is_tv") else "Movie",
        "ts": 0, "duration": 0, "single_child_guid": "", "file_name": "",
    }


def detail_item(b: dict) -> dict:
    ov = cms.clean_text(b.get("overview") or "")
    extra = []
    if b.get("type_name"):
        extra.append(f"分类：{b['type_name']}")
    if b.get("actor"):
        extra.append(f"主演：{b['actor']}")
    if b.get("director"):
        extra.append(f"导演：{b['director']}")
    if b.get("updated"):
        extra.append(f"更新：{b['updated']}")
    if extra:
        ov = (ov + "\n\n" if ov else "") + "\n".join(extra)
    poster = poster_path(b.get("poster", ""))
    return {
        "guid": b["guid"], "imdb_id": "", "trim_id": "", "tv_title": "",
        "parent_title": "", "title": b["title"], "logos": "",
        "poster": poster,
        "original_title": "", "backdrops": poster, "posters": poster,
        "poster_width": 0, "poster_height": 0,
        "vote_average": str(b.get("score") or "0"), "genres": [],
        "content_ratings": "", "release_date": b.get("year") or "",
        "runtime": 0, "production_countries": [b["area"]] if b.get("area") else [],
        "overview": ov, "is_favorite": 0, "is_watched": 0, "watched_ts": 0,
        "air_date": b.get("year") or "", "season_number": 0,
        "number_of_seasons": 1 if b.get("is_tv") else 0,
        "number_of_episodes": b.get("episodes", 0),
        "local_number_of_episodes": b.get("episodes", 0) if b.get("is_tv") else 0,
        "local_number_of_seasons": 1 if b.get("is_tv") else 0,
        "can_play": 1, "type": "TV" if b.get("is_tv") else "Movie",
        "play_error": "", "parent_guid": "", "ancestor_guid": "",
        "ancestor_name": "🌐在线资源",
        "ancestor_category": "TV" if b.get("is_tv") else "Movie",
        "play_item_guid": b["guid"], "duration": 0, "logic_type": 0,
        "status": "在线",
        "media_stream": {"resolutions": None, "audio_type": None,
                         "color_range_type": None},
    }


def _base_item(**kw):
    row = {
        "guid": "", "lan": "zh-CN", "imdb_id": "", "trim_id": "", "tv_title": "",
        "parent_guid": "", "parent_title": "", "title": "", "type": "Episode",
        "poster": "", "poster_width": 0, "poster_height": 0, "is_favorite": 0,
        "watched": 0, "watched_ts": 0, "vote_average": "0",
        "media_stream": {"resolutions": None, "audio_type": None,
                         "color_range_type": None},
        "release_date": "", "season_number": 1, "episode_number": 0,
        "air_date": "", "number_of_seasons": 0, "number_of_episodes": 0,
        "local_number_of_seasons": 0, "local_number_of_episodes": 0,
        "status": "1", "ancestor_guid": "", "ancestor_name": "🌐在线资源",
        "ancestor_category": "TV", "ts": 0, "duration": 0,
        "single_child_guid": "", "file_name": "", "overview": "", "genres": [],
        "path": "", "runtime": 0,
    }
    row.update(kw)
    return row


def season_list(b: dict) -> list[dict]:
    if not b.get("is_tv"):
        return []
    return [_base_item(
        guid=season_guid(b["guid"]), type="Season", title="第 1 季",
        parent_guid=b["guid"], parent_title=b["title"], tv_title=b["title"],
        season_number=1, number_of_seasons=1, can_play=None,
        poster=poster_path(b.get("poster", "")),
    )]


def _episode_row(b: dict, eps: list, index: int) -> dict:
    ep = eps[index] if 0 <= index < len(eps) else {}
    eg = episode_guid(b["guid"], index)
    poster = poster_path(b.get("poster", ""))
    year = b.get("year") or ""
    return _base_item(
        guid=eg, type="Episode", title=ep.get("title") or f"第{index + 1}集",
        parent_guid=season_guid(b["guid"]), parent_title="第 1 季",
        tv_title=b["title"], season_number=1, episode_number=index + 1,
        number_of_seasons=1, can_play=1, play_item_guid=eg, play_error="",
        overview=str(b.get("overview") or "")[:1000],
        release_date=year, air_date=year,
        vote_average=str(b.get("score") or "0"),
        poster=poster, posters=poster, backdrops=poster,
        media_guid=ep_media_guid(eg), video_guid=ep_video_guid(eg),
        audio_guid="", subtitle_guid="",
    )


def episode_item(b: dict, index: int) -> dict:
    names = b.get("line_names") or []
    eps = b["lines"].get(names[0], []) if names else []
    return _episode_row(b, eps, index)


def season_item(b: dict) -> dict:
    rows = season_list(b)
    if not rows:
        return {}
    row = rows[0]
    total = len(episode_list(b))
    row["number_of_episodes"] = total
    row["local_number_of_episodes"] = total
    return row


def episode_list(b: dict) -> list[dict]:
    if not b.get("is_tv"):
        return []
    names = b.get("line_names") or []
    eps = b["lines"].get(names[0], []) if names else []
    return [_episode_row(b, eps, i) for i in range(len(eps))]


_TIERS = ((2160, "4k"), (1440, "2k"), (1080, "1080p"), (720, "720p"),
          (480, "480p"), (360, "360p"), (240, "240p"))


def resolution_type(height: int) -> str:
    for value, label in _TIERS:
        if height >= value * 0.85:
            return label
    return "240p"


def stream_list(b: dict, mg: str, vg: str, meta: dict | None = None) -> dict:
    ext = b.get("primary_ext") or "m3u8"
    wrapper = "MP4" if ext == "mp4" else "HLS"
    width, height, bps = 1920, 1080, 0
    resolution = "1080p"
    if meta:
        meta_width = int(meta.get("width") or 0)
        meta_height = int(meta.get("height") or 0)
        if meta_height > 0:
            width, height = meta_width, meta_height
            resolution = resolution_type(min(meta_width, meta_height))
        meta_bps = int(meta.get("bandwidth") or 0)
        if meta_bps > 0:
            bps = meta_bps
    return {
        "files": [{
            "guid": mg, "path": f"在线/{b['title']}.{ext}",
            "file_name": b["title"] + f".{ext}", "size": 0, "timestamp": 0,
            "type": 1, "sort_num": 0, "can_play": 1, "play_error": "",
            "create_time": 0, "update_time": 0, "parent_fv_guid": "",
            "file_birth_time": 0, "progress_thumb_hash_dir": "",
        }],
        "video_streams": [{
            "media_guid": mg, "title": "", "guid": vg,
            "resolution_type": resolution, "color_range_type": "SDR",
            "codec_name": "h264", "codec_type": "video", "color_range": "tv",
            "profile": "High", "index": 0, "width": width, "height": height,
            "coded_width": 0, "coded_height": 0, "display_aspect_ratio": "16:9",
            "pix_fmt": "yuv420p", "level": "40", "color_space": "",
            "color_transfer": "", "color_primaries": "",
            "duration": int((meta or {}).get("duration") or 0) if isinstance(meta, dict) else 0,
            "dv_profile": 0, "refs": 1, "r_frame_rate": "25 fps",
            "avg_frame_rate": "25 fps", "bits_per_raw_sample": "8", "bps": bps,
            "progressive": 1, "bit_depth": 8, "wrapper": wrapper,
            "create_time": 0, "update_time": 0, "rotation": 0, "ext1": 0,
            "is_bluray": False,
        }],
        "audio_streams": [], "subtitle_streams": [],
    }


_LINE_BR_BASE = 90_000_000
_LINE_BR_OFFSET = 100_020
_LINE_BR_STEP = 10_001
_LINE_BR_SPAN = 800_000


def line_code(name: str) -> int:
    digest = hashlib.md5(str(name).encode("utf-8")).hexdigest()[:8]
    return _LINE_BR_BASE + _LINE_BR_OFFSET + int(digest, 16) % _LINE_BR_SPAN


def line_index(value, names: list[str]) -> int | None:
    if not names:
        return None
    raw = str(value if value is not None else "").strip()
    if not raw:
        return None
    legacy = re.fullmatch(r"L(\d+)", raw, re.I)
    if legacy:
        idx = int(legacy.group(1))
        return idx if 0 <= idx < len(names) else None
    try:
        code = int(raw)
    except ValueError:
        try:
            code = int(float(raw))
        except ValueError:
            return None
    if _LINE_BR_BASE <= code < 91_000_000:
        if code >= _LINE_BR_BASE + _LINE_BR_OFFSET:
            for i, name in enumerate(names):
                if line_code(name) == code:
                    return i
            return None
        idx = (code - _LINE_BR_BASE) // _LINE_BR_STEP
        return idx if 0 <= idx < len(names) else None
    if 0 <= code < len(names):
        return code
    return None


_LINE_LABELS = {
    "lzm3u8": "量子", "tpm3u8": "淘片", "hhm3u8": "豪华",
    "ffm3u8": "非凡", "wjm3u8": "无尽", "snm3u8": "丝诺",
    "dyttm3u8": "电影天堂", "bfzym3u8": "暴风", "360zy": "360",
}


def line_label(name: str) -> str:
    return _LINE_LABELS.get(str(name).lower(), str(name))


def line_display(b: dict, name: str) -> str:
    raw = str(name or "")
    if "：" in raw:
        prefix, _, suffix = raw.partition("：")
        return f"{prefix}：{line_label(suffix)}"
    label = line_label(raw)
    provider = str(b.get("provider_name") or b.get("provider") or "").strip()
    return f"{provider}：{label}" if provider else label


def qualities(b: dict) -> list[dict]:
    names = b.get("line_names") or []
    if len(names) < 2:
        return []
    return [{"resolution": line_display(b, name), "bitrate": line_code(name),
             "progressive": True, "is_m3u8": False} for name in names]


def stream_list_payload(b: dict, mg: str, vg: str,
                        meta: dict | None = None) -> dict:
    data = stream_list(b, mg, vg, meta)
    quals = qualities(b)
    if quals:
        data["video_streams"][0]["resolution_type"] = quals[0]["resolution"]
        data["video_streams"][0]["bps"] = quals[0]["bitrate"]
    data["qualities"] = quals
    data["cloud_storage_info"] = None
    return data


def stream_payload(b: dict, mg: str, vg: str, meta: dict | None = None,
                   line: int = 0) -> dict:
    data = stream_list(b, mg, vg, meta)
    video = data["video_streams"][0]
    quals = qualities(b)
    if quals:
        line = max(0, min(int(line or 0), len(quals) - 1))
        video["resolution_type"] = quals[line]["resolution"]
        video["bps"] = quals[line]["bitrate"]
    else:
        quals = [{
            "resolution": video["resolution_type"] or "原画",
            "bitrate": video["bps"],
            "progressive": True,
            "is_m3u8": False,
        }]
    return {
        "file_stream": data["files"][0], "video_stream": video,
        "audio_streams": [], "subtitle_streams": [],
        "qualities": quals, "cloud_storage_info": None,
    }


def _split_person_names(value) -> list:
    return [n.strip() for n in re.split(r"[/、,，;；]", str(value or ""))
            if n.strip()][:30]


def _person_row(guid: str, name: str, job: str, role: str, order: int) -> dict:
    return {
        "item_guid": guid, "person_guid": "", "role": role, "job": job,
        "order": order, "department": "", "trim_id": "", "imdb_id": "",
        "tmdb_id": 0, "lan": "zh-CN", "name": name, "original_name": name,
        "also_know_as": "", "biography": "", "known_for_department": "",
        "profile_path": "", "gender": 0,
    }


def person_list(b: dict) -> dict:
    out = []
    order = 1
    for name in _split_person_names(b.get("director") or ""):
        out.append(_person_row(b.get("guid", ""), name, "Director", "", order))
        order += 1
    for name in _split_person_names(b.get("actor") or ""):
        out.append(_person_row(b.get("guid", ""), name, "Actor", "", order))
        order += 1
    return {"total": len(out), "list": out[:30]}


def play_info(b: dict, mg: str, vg: str, guid: str) -> dict:
    return {
        "grand_guid": "", "guid": guid, "parent_guid": "",
        "play_config": None, "ts": 0,
        "type": "TV" if b.get("is_tv") else "Movie",
        "video_guid": vg, "audio_guid": "", "subtitle_guid": "",
        "media_guid": mg, "item": detail_item(b),
        "direct_link_audio_index": -1,
    }


def episode_play_info(b: dict, index: int, guid: str) -> dict:
    return {
        "grand_guid": "", "guid": guid,
        "parent_guid": season_guid(b["guid"]),
        "play_config": None, "ts": 0, "type": "Episode",
        "video_guid": ep_video_guid(guid), "audio_guid": "", "subtitle_guid": "",
        "media_guid": ep_media_guid(guid), "item": episode_item(b, index),
        "direct_link_audio_index": -1,
    }


def history_card(b: dict, rec: dict) -> dict:
    row = search_card(b)
    pos = int(rec.get("ts") or 0)
    row["ts"] = pos
    # 继续观看卡片的进度条读 watched_ts（与 play/info 的续播字段同一语义）。
    row["watched_ts"] = pos
    row["duration"] = int(rec.get("duration") or 0)
    row["_updated_at"] = int(rec.get("updated_at") or 0)
    return row


def play_response(mg: str, vg: str, link: str, wrapper: str = "HLS") -> dict:
    return {
        "code": 0, "msg": "", "data": {
            "audio_guid": "", "audio_index": -1, "hls_time": 6,
            "is_subtitle_external": 0, "media_guid": mg,
            "play_link": link, "subtitle_guid": "", "subtitle_index": -1,
            "subtitle_link": "", "video_guid": vg, "video_index": 0,
            "wrapper": wrapper, "non_fatal_errno": None, "video_encoder": "",
            "not_supported_resolution": "", "supported_highest_resolution": "",
        },
    }


def ok(data) -> dict:
    return {"code": 0, "msg": "", "data": data}
