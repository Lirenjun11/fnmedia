#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import time
import urllib.parse

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

import config
import netguard

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
_URI_ATTR = re.compile(r'URI="([^"]+)"')


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
    return text, base, build_playlist_meta(text)


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
