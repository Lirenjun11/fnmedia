#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import time
import urllib.parse
from collections import deque

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

import config
import netguard
import runtime

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
_URI_ATTR = re.compile(r'URI="([^"]+)"')

_AD_PLACEHOLDER = "__FNMEDIA_AD_SEGMENT__"
_AD_RANGE_RE = re.compile(r"(?i)\b(ad|ads|advert|advertisement|commercial)\b")
_AD_KEEP_TAGS = (
    "#EXTM3U", "#EXT-X-VERSION", "#EXT-X-TARGETDURATION",
    "#EXT-X-MEDIA-SEQUENCE", "#EXT-X-PLAYLIST-TYPE",
    "#EXT-X-INDEPENDENT-SEGMENTS", "#EXT-X-START", "#EXT-X-ENDLIST",
    "#EXT-X-DISCONTINUITY-SEQUENCE", "#EXT-X-KEY", "#EXT-X-ALLOW-CACHE",
)
_AD_MAX_REMOVE_RATIO = 0.5
_AD_MAX_REMOVED = 40
_AD_MAX_BREAK_RATIO = 0.20
_AD_MAX_GROUPS = 10
_AD_MIN_GROUP_SEGMENTS = 3
_AD_MAX_GROUP_SEGMENTS = 15
_AD_MIN_GROUP_DURATION = 3.0
_AD_MAX_GROUP_DURATION = 120.0
_AD_MAX_GROUP_RELATIVE = 0.05
_AD_PREVIEWS: deque = deque(maxlen=5)


def _extinf_duration(line: str) -> float:
    stripped = line.strip()
    if not stripped.startswith("#EXTINF:"):
        return 0.0
    try:
        return max(0.0, float(stripped[8:].split(",", 1)[0]))
    except ValueError:
        return 0.0


def ad_rules() -> dict:
    cfg = config.get()
    keywords = cfg.get("adfilter_keywords") or []
    if not isinstance(keywords, list):
        keywords = []
    try:
        head_skip = float(cfg.get("adfilter_head_skip") or 0)
    except (TypeError, ValueError):
        head_skip = 0.0
    return {
        "enabled": bool(cfg.get("adfilter_enabled")),
        "markers": bool(cfg.get("adfilter_markers", True)),
        "keywords": [str(word).strip().lower() for word in keywords[:32]
                     if str(word).strip()],
        "head_skip": max(0.0, min(120.0, head_skip)),
        "head_guard": bool(cfg.get("adfilter_head_guard", True)),
        "breaks": bool(cfg.get("adfilter_breaks", True)),
        "strong": bool(cfg.get("adfilter_strong", True)),
    }


def ad_rules_active(rules: dict) -> bool:
    return bool(rules["enabled"] and (
        rules["markers"] or rules["keywords"] or rules["head_skip"] > 0
        or rules["breaks"] or rules["strong"]))


def _same_stream(left: str, right: str) -> bool:
    if not left or not right:
        return False
    a, b = urllib.parse.urlsplit(left), urllib.parse.urlsplit(right)
    if a.scheme != b.scheme or a.netloc != b.netloc:
        return False
    return a.path.rsplit("/", 1)[0] == b.path.rsplit("/", 1)[0]


def recent_ad_previews(limit: int = 3) -> list[dict]:
    return list(_AD_PREVIEWS)[-max(1, min(int(limit), 5)):]


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).netloc.lower()
    except ValueError:
        return ""


def _is_tracking_segment(url: str, duration: float) -> bool:
    if not 0 < duration < 0.5:
        return False
    path = urllib.parse.urlsplit(url).path.lower()
    return not path.endswith((".ts", ".m4s", ".mp4", ".m4a", ".aac", ".mp3"))


def _group_summary(records: list[dict]) -> dict[int, dict]:
    groups: dict[int, dict] = {}
    for rec in records:
        info = groups.setdefault(rec["group"], {"count": 0, "duration": 0.0})
        info["count"] += 1
        info["duration"] += rec["duration"]
    return groups


def strip_ads(text: str, base_url: str = "",
              rules: dict | None = None) -> tuple[str, dict]:
    """Drop ad segments from a media playlist, keeping AES-128 indexes intact.

    Removed segments leave a placeholder URI line so rewrite_playlist still
    advances its sequence counter; tag lines are dropped except global tags
    and #EXT-X-KEY, which must survive to keep the key scope of later segments.
    """
    rules = rules or ad_rules()
    stats = {"removed": 0, "removed_duration": 0.0, "kept_duration": 0.0,
             "total_duration": 0.0, "by_marker": 0, "by_keyword": 0,
             "by_head": 0, "by_break": 0, "by_duplicate": 0, "by_tiny": 0,
             "by_minority": 0, "guard_kept": 0, "guard": "", "sample": "",
             "dropped": [], "groups": [], "disc_cleaned": 0}
    lines = text.splitlines()
    total_duration = sum(_extinf_duration(row) for row in lines)
    stats["total_duration"] = total_duration
    if not ad_rules_active(rules) or is_master(text):
        stats["kept_duration"] = total_duration
        return text, stats

    head_active = rules["head_skip"] > 0
    if head_active and "#EXT-X-ENDLIST" not in text:
        head_active = False
        stats["guard"] = "head skip ignored: live playlist"

    records: list[dict] = []
    pending: list[str] = []
    cue_ad = False
    marker_hit = False
    elapsed = 0.0
    group = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            upper = stripped.upper()
            if upper == "#EXT-X-DISCONTINUITY":
                group += 1
            if rules["markers"]:
                if upper.startswith("#EXT-X-CUE-OUT"):
                    cue_ad = True
                elif upper.startswith("#EXT-X-CUE-IN"):
                    cue_ad = False
                elif upper.startswith("#EXT-X-DATERANGE") and _AD_RANGE_RE.search(stripped):
                    marker_hit = True
            pending.append(line)
            continue
        block, pending = pending, []
        duration = sum(_extinf_duration(row) for row in block)
        absolute = urllib.parse.urljoin(base_url, stripped) if base_url else stripped
        reason = ""
        if rules["markers"] and (cue_ad or marker_hit):
            reason = "marker"
        elif rules["keywords"] and any(word in absolute.lower()
                                       for word in rules["keywords"]):
            reason = "keyword"
        elif head_active and elapsed < rules["head_skip"]:
            reason = "head"
        elapsed += duration
        records.append({"tags": block, "uri": line, "abs": absolute,
                        "duration": duration, "reason": reason,
                        "group": group, "host": _host_of(absolute)})
        marker_hit = False
    tail = pending

    # Strong evidence rules share one switch: all three are unconditional
    # inferences with the same false-positive risk, separate from grouping.
    if rules["strong"]:
        seen: set[str] = set()
        for rec in records:
            if rec["reason"]:
                continue
            if rec["abs"] in seen:
                rec["reason"] = "duplicate"
            elif _is_tracking_segment(rec["abs"], rec["duration"]):
                rec["reason"] = "tiny"
            seen.add(rec["abs"])

        # A host appearing at most twice in a long playlist is usually an
        # injected ad server, not a CDN mirror.
        if len(records) >= 20:
            counts: dict[str, int] = {}
            for rec in records:
                counts[rec["host"]] = counts.get(rec["host"], 0) + 1
            main_host = max(counts, key=counts.get) if counts else ""
            for rec in records:
                if (not rec["reason"] and rec["host"] != main_host
                        and counts[rec["host"]] <= 2):
                    rec["reason"] = "minority"

    # Conservative discontinuity-group heuristic: only short groups next to
    # much longer content groups, with several conditions all true.
    groups = _group_summary(records)
    stats["groups"] = [
        {"group": g, "count": info["count"],
         "duration": round(info["duration"], 2)}
        for g, info in sorted(groups.items())
    ]
    if rules["breaks"] and 2 <= len(groups) <= _AD_MAX_GROUPS:
        longest = max((info["duration"] for info in groups.values()), default=0.0)
        if longest > 0:
            for g, info in groups.items():
                if not (_AD_MIN_GROUP_SEGMENTS <= info["count"]
                        <= _AD_MAX_GROUP_SEGMENTS):
                    continue
                if not (_AD_MIN_GROUP_DURATION <= info["duration"]
                        <= _AD_MAX_GROUP_DURATION):
                    continue
                if info["duration"] / longest > _AD_MAX_GROUP_RELATIVE:
                    continue
                for rec in records:
                    if rec["group"] == g and not rec["reason"]:
                        rec["reason"] = "break"
            break_recs = [rec for rec in records if rec["reason"] == "break"]
            break_duration = sum(rec["duration"] for rec in break_recs)
            if (break_recs and total_duration > 0 and
                    break_duration / total_duration > _AD_MAX_BREAK_RATIO):
                for rec in break_recs:
                    rec["reason"] = ""
                stats["guard"] = "break groups exceed 20%, reverted"

    # A head-only hit that shares host and directory with kept content is
    # almost always the first real segment, not an ad break.
    if rules["head_guard"]:
        has_feature = any(rec["reason"] in ("marker", "keyword")
                          for rec in records)
        if not has_feature:
            reference = next((rec["abs"] for rec in records
                              if rec["reason"] == ""), "")
            for rec in records:
                if rec["reason"] == "head" and _same_stream(reference, rec["abs"]):
                    rec["reason"] = ""
                    stats["guard_kept"] += 1
            if stats["guard_kept"] and not stats["guard"]:
                stats["guard"] = "head-only suspect segments kept"

    removed = [rec for rec in records if rec["reason"]]
    removed_duration = sum(rec["duration"] for rec in removed)
    if removed and (len(removed) > _AD_MAX_REMOVED or
                    (total_duration > 0 and
                     removed_duration / total_duration > _AD_MAX_REMOVE_RATIO)
                    or len(removed) == len(records)):
        for rec in removed:
            rec["reason"] = ""
        stats["guard"] = "bulk removal reverted"
        stats["guard_kept"] += len(removed)
        removed = []

    if not removed:
        stats["kept_duration"] = total_duration
        return text, stats

    out: list[str] = []
    for index, rec in enumerate(records):
        if rec["reason"]:
            stats["removed"] += 1
            stats["removed_duration"] += rec["duration"]
            stats["by_" + rec["reason"]] += 1
            if not stats["sample"]:
                stats["sample"] = rec["abs"][:100]
            if len(stats["dropped"]) < 20:
                stats["dropped"].append({
                    "url": rec["abs"][:160],
                    "duration": round(rec["duration"], 2),
                    "reason": rec["reason"],
                    "group": rec["group"],
                })
            out.extend(row for row in rec["tags"]
                       if row.strip().upper().startswith(_AD_KEEP_TAGS))
            out.append(_AD_PLACEHOLDER)
        else:
            stats["kept_duration"] += rec["duration"]
            prev_removed = index > 0 and bool(records[index - 1]["reason"])
            tags = []
            for row in rec["tags"]:
                upper = row.strip().upper()
                if (upper.startswith("#EXT-X-CUE-")
                        or upper.startswith("#EXT-X-DATERANGE")):
                    continue
                # A discontinuity right after a removed ad group is an ad
                # exit marker; keeping it makes players re-init decoders.
                if upper == "#EXT-X-DISCONTINUITY" and prev_removed:
                    stats["disc_cleaned"] += 1
                    continue
                tags.append(row)
            out.extend(tags)
            out.append(rec["uri"])
    out.extend(tail)
    return "\n".join(out) + "\n", stats


def encode_url(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def decode_url(token: str) -> str:
    padded = token + "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(padded).decode()


_OPENSSL = shutil.which("openssl")


def can_decrypt() -> bool:
    return bool(_OPENSSL)


def encode_seg(url: str, key_url: str = "", iv: str = "") -> str:
    if not key_url:
        return encode_url(url)
    raw = json.dumps({"u": url, "k": key_url, "iv": iv}, separators=(",", ":"))
    return "k" + base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_seg(token: str) -> tuple[str, str, str]:
    if not token.startswith("k"):
        return decode_url(token), "", ""
    padded = token[1:] + "=" * (-len(token[1:]) % 4)
    data = json.loads(base64.urlsafe_b64decode(padded).decode())
    return str(data.get("u", "")), str(data.get("k", "")), str(data.get("iv", ""))


_KEYS: dict = {}
_KEY_TTL = 120.0


async def fetch_key(client, url: str, timeout: float = 8.0) -> bytes:
    now = time.monotonic()
    hit = _KEYS.get(url)
    if hit and now - hit[0] < _KEY_TTL:
        return hit[1]
    resp = await client.get(url, headers={"User-Agent": _UA}, timeout=timeout)
    resp.raise_for_status()
    _KEYS[url] = (now, resp.content)
    if len(_KEYS) > 300:
        for stale in list(_KEYS)[:60]:
            _KEYS.pop(stale, None)
    return resp.content


async def fetch_media_bytes(client, url: str, timeout: float = 15.0) -> bytes:
    parsed = urllib.parse.urlparse(url)
    resp = await client.get(url, timeout=timeout, headers={
        "User-Agent": _UA,
        "Referer": f"{parsed.scheme}://{parsed.netloc}/",
    })
    resp.raise_for_status()
    return resp.content


def decrypt_segment(data: bytes, key: bytes, iv: str) -> bytes | None:
    if not _OPENSSL or len(key) != 16:
        return None
    iv_hex = (iv or "0" * 32).removeprefix("0x").ljust(32, "0")[:32]
    try:
        proc = subprocess.run(
            [_OPENSSL, "enc", "-d", "-aes-128-cbc", "-K", key.hex(),
             "-iv", iv_hex],
            input=data, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def is_playlist(text: str) -> bool:
    head = (text or "").lstrip()[:64]
    return head.startswith("#EXTM3U") or "#EXTINF" in (text or "")[:2048]


_M3U8_RE = re.compile(r'''(?:https?:)?//[^\s"'<>\\]+?\.m3u8[^\s"'<>\\]*''')
_REL_M3U8_RE = re.compile(r'''["']([^"']+?\.m3u8[^"']*)["']''')


def extract_m3u8(text: str) -> str:
    """分享播放页（HTML/JSON）里硬编码的 m3u8 地址。"""
    plain = (text or "").replace("\\/", "/")
    for pattern in (_M3U8_RE, _REL_M3U8_RE):
        match = pattern.search(plain)
        if match:
            return match.group(1) if pattern is _REL_M3U8_RE else match.group(0)
    return ""


def ensure_public_target(url: str) -> str:
    """分片、图片与直链代理的目标校验。

    默认允许局域网 / 回环地址以内网自建源；可在设置中关闭
    allow_intranet 恢复仅公网目标。链路本地与保留地址始终拦截。
    """
    allow_intranet = bool(config.get().get("allow_intranet", True))
    return netguard.validate_target(url, allow_intranet=allow_intranet,
                                    label="媒体地址")


def parse_stream_inf(text: str) -> list[dict]:
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("#EXT-X-STREAM-INF"):
            continue
        attrs = {}
        for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', stripped):
            attrs[match.group(1)] = match.group(2).strip('"')
        uri = ""
        for nxt in lines[i + 1:]:
            candidate = nxt.strip()
            if not candidate:
                continue
            if not candidate.startswith("#"):
                uri = candidate
            break
        width = height = 0
        res = attrs.get("RESOLUTION", "")
        if "x" in res:
            try:
                width, height = int(res.split("x", 1)[0]), int(res.split("x", 1)[1])
            except ValueError:
                width = height = 0
        try:
            bandwidth = int(attrs.get("BANDWIDTH", "0") or 0)
        except ValueError:
            bandwidth = 0
        out.append({"uri": uri, "bandwidth": bandwidth,
                    "width": width, "height": height})
    return out


def build_playlist_meta(text: str, child: dict | None = None) -> dict:
    meta = {"width": 0, "height": 0, "bandwidth": 0, "variant_count": 0,
            "duration": 0.0, "has_endlist": True}
    meta.update({k: v for k, v in (child or {}).items() if v is not None})
    if is_master(text):
        variants = parse_stream_inf(text)
        meta["variant_count"] = len(variants)
        best = max(variants, key=lambda v: v["bandwidth"], default=None)
        if best:
            meta["bandwidth"] = best["bandwidth"]
            if best["width"] and best["height"]:
                meta["width"] = max(best["width"], best["height"])
                meta["height"] = min(best["width"], best["height"])
    else:
        duration = 0.0
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#EXTINF:"):
                try:
                    duration += float(stripped[8:].split(",")[0])
                except ValueError:
                    pass
        meta["duration"] = duration
        meta["has_endlist"] = "#EXT-X-ENDLIST" in text
    return meta


_PLAYLISTS: dict = {}
_PLAYLIST_TTL = 120.0


def remember_playlist_text(key: tuple, text: str, base: str, meta: dict) -> None:
    _PLAYLISTS[key] = (time.monotonic(), text, base, meta)
    if len(_PLAYLISTS) > 400:
        for stale in list(_PLAYLISTS)[:80]:
            _PLAYLISTS.pop(stale, None)


def get_playlist_text(key: tuple) -> tuple[str, str, dict] | None:
    hit = _PLAYLISTS.get(key)
    if not hit:
        return None
    ts, text, base, meta = hit
    if time.monotonic() - ts > _PLAYLIST_TTL:
        _PLAYLISTS.pop(key, None)
        return None
    return text, base, meta


_META: dict = {}
_META_TTL = 120.0


def remember_playlist_meta(key: tuple, meta: dict) -> None:
    _META[key] = (time.monotonic(), meta)
    if len(_META) > 500:
        for stale in list(_META)[:100]:
            _META.pop(stale, None)


def get_playlist_meta(key: tuple) -> dict | None:
    hit = _META.get(key)
    if not hit:
        return None
    ts, meta = hit
    if time.monotonic() - ts > _META_TTL:
        _META.pop(key, None)
        return None
    return meta


def pick_best_variant(text: str) -> str | None:
    variants = [v for v in parse_stream_inf(text) if v["uri"]]
    if not variants:
        return None
    return max(variants, key=lambda v: v["bandwidth"])["uri"]


def rewrite_playlist(text: str, base_url: str, seg_prefix: str,
                     variant_prefix: str = "", is_master: bool = False,
                     decrypt_keys: bool = False) -> str:
    if not is_master and ad_rules_active(ad_rules()):
        original = text
        text, ad_stats = strip_ads(text, base_url)
        if ad_stats["removed"] or ad_stats["guard_kept"] or ad_stats["guard"]:
            runtime.log(
                f"adfilter: removed={ad_stats['removed']} "
                f"cut={ad_stats['removed_duration']:.0f}s "
                f"kept={ad_stats['kept_duration']:.0f}s "
                f"total={ad_stats['total_duration']:.0f}s "
                f"hits=m{ad_stats['by_marker']}/k{ad_stats['by_keyword']}/"
                f"h{ad_stats['by_head']}/b{ad_stats['by_break']}/"
                f"d{ad_stats['by_duplicate']}/t{ad_stats['by_tiny']}/"
                f"o{ad_stats['by_minority']} "
                f"guard={ad_stats['guard'] or '-'} "
                f"guarded={ad_stats['guard_kept']} "
                f"disc={ad_stats['disc_cleaned']} "
                f"sample={ad_stats['sample'][:80]}")
            _AD_PREVIEWS.append({
                "ts": time.time(),
                "removed": ad_stats["removed"],
                "removed_duration": round(ad_stats["removed_duration"], 2),
                "kept_duration": round(ad_stats["kept_duration"], 2),
                "total_duration": round(ad_stats["total_duration"], 2),
                "by_marker": ad_stats["by_marker"],
                "by_keyword": ad_stats["by_keyword"],
                "by_head": ad_stats["by_head"],
                "by_break": ad_stats["by_break"],
                "by_duplicate": ad_stats["by_duplicate"],
                "by_tiny": ad_stats["by_tiny"],
                "by_minority": ad_stats["by_minority"],
                "groups": ad_stats["groups"],
                "disc_cleaned": ad_stats["disc_cleaned"],
                "guard": ad_stats["guard"],
                "guard_kept": ad_stats["guard_kept"],
                "sample": ad_stats["sample"],
                "dropped": ad_stats["dropped"],
                "raw_head": "\n".join(original.splitlines()[:40]),
                "kept_head": "\n".join(text.splitlines()[:40]),
            })
    out = []
    key_url = ""
    key_iv = ""
    seq = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if not is_master and stripped.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                seq = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                seq = 0
        if not is_master and decrypt_keys and stripped.startswith("#EXT-X-KEY"):
            upper = stripped.upper()
            uri = _URI_ATTR.search(stripped)
            if "METHOD=AES-128" in upper and uri:
                key_url = urllib.parse.urljoin(base_url, uri.group(1))
                iv = re.search(r"IV=0x([0-9A-Fa-f]+)", stripped)
                key_iv = iv.group(1) if iv else ""
                continue
            key_url = ""
            key_iv = ""
        if stripped.startswith("#"):
            attr_target = variant_prefix if is_master else seg_prefix

            def sub(match):
                absolute = urllib.parse.urljoin(base_url, match.group(1))
                return f'URI="{attr_target}?u={encode_url(absolute)}"'
            out.append(_URI_ATTR.sub(sub, line))
            continue
        if stripped == _AD_PLACEHOLDER:
            seq += 1
            continue
        target = variant_prefix if is_master else seg_prefix
        absolute = urllib.parse.urljoin(base_url, stripped)
        if key_url:
            iv = key_iv or f"{seq:032x}"
            out.append(f"{target}?u={encode_seg(absolute, key_url, iv)}")
        else:
            out.append(f"{target}?u={encode_url(absolute)}")
        seq += 1
    return "\n".join(out) + "\n"


async def fetch_playlist(client: httpx.AsyncClient, url: str,
                         timeout: float = 3.0,
                         depth: int = 0) -> tuple[str, str, dict]:
    parsed = urllib.parse.urlparse(url)
    headers = {"User-Agent": _UA,
               "Referer": f"{parsed.scheme}://{parsed.netloc}/"}
    resp = await client.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    text = resp.text
    base = str(resp.url)
    if not is_playlist(text):
        if depth < 2:
            child = extract_m3u8(text)
            if child:
                return await fetch_playlist(
                    client, ensure_public_target(urllib.parse.urljoin(base, child)),
                    timeout, depth + 1)
        raise ValueError("not a playlist")
    if is_master(text) and depth < 2:
        best = pick_best_variant(text)
        if best:
            child_text, child_base, child_meta = await fetch_playlist(
                client, urllib.parse.urljoin(base, best), timeout, depth + 1)
            return child_text, child_base, build_playlist_meta(text, child_meta)
    meta = build_playlist_meta(text)
    rules = ad_rules()
    if ad_rules_active(rules) and not is_master(text):
        _, ad_stats = strip_ads(text, base, rules)
        meta["duration"] = ad_stats["kept_duration"]
    return text, base, meta


def playlist_response(text: str, base: str, seg_prefix: str,
                      variant_prefix: str = "",
                      is_master: bool = False,
                      decrypt_keys: bool = False) -> Response:
    return Response(rewrite_playlist(text, base, seg_prefix, variant_prefix,
                                     is_master, decrypt_keys),
                    media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store"})


async def proxy_media(request: Request, url: str, client: httpx.AsyncClient,
                      timeout: float = 8.0) -> StreamingResponse | Response:
    headers = {"User-Agent": _UA}
    for key in ("range", "if-range", "if-none-match"):
        if key in request.headers:
            headers[key] = request.headers[key]
    parsed = urllib.parse.urlparse(url)
    # 不能把播放器（我们自己的 NAS 地址）的 Referer 透传给源站，
    # 分享页的密钥/分片服务器会因此返回无效内容。
    headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    resp = await client.send(client.build_request("GET", url, headers=headers),
                             stream=True)
    out_headers = {}
    for key in ("content-type", "content-range", "accept-ranges", "etag"):
        if key in resp.headers:
            out_headers[key] = resp.headers[key]
    out_headers["Cache-Control"] = "public, max-age=3600"
    status = resp.status_code
    if status >= 400:
        await resp.aclose()
        return Response(status_code=status, headers=out_headers)

    async def chunks():
        try:
            async for block in resp.aiter_bytes(65536):
                yield block
        finally:
            await resp.aclose()

    return StreamingResponse(chunks(), status_code=status, headers=out_headers,
                             media_type=out_headers.get("content-type"))


def _poster_fallback() -> Response:
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="2" height="3">'
           b'<rect width="2" height="3" fill="#1b2130"/></svg>')
    return Response(svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=600"})


async def proxy_poster(url: str, client: httpx.AsyncClient) -> Response:
    parsed = urllib.parse.urlparse(url)
    headers = {"User-Agent": _UA}
    host = parsed.netloc.lower()
    if host.endswith("doubanio.com") or host.endswith("douban.com"):
        headers["Referer"] = "https://movie.douban.com/"
    else:
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    try:
        resp = await client.get(url, headers=headers, timeout=5.0)
        if resp.status_code != 200 or not resp.content:
            return _poster_fallback()
        head = resp.content[:12]
        if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
            ctype = "image/webp"
        elif head[:8] == b"\x89PNG\r\n\x1a\n":
            ctype = "image/png"
        elif head[:3] == b"\xff\xd8\xff":
            ctype = "image/jpeg"
        elif head[:6] in (b"GIF87a", b"GIF89a"):
            ctype = "image/gif"
        else:
            declared = (resp.headers.get("content-type") or "").lower()
            ctype = declared if declared.startswith("image/") else "image/jpeg"
        return Response(resp.content, media_type=ctype,
                        headers={"Cache-Control": "public, max-age=86400",
                                 "Access-Control-Allow-Origin": "*"})
    except Exception:
        return _poster_fallback()
