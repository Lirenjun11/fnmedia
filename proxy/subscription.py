#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.parse
from typing import Any

import httpx

import config
import netguard

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}
_CMS_HINT = re.compile(r"provide/vod|apijson|api\.php|/at/json|/at/xml", re.I)
_ADULT_HINT = "🔞"


def b58decode(text: str) -> bytes:
    s = text.strip()
    if not s:
        raise ValueError("empty base58")
    n = 0
    for ch in s:
        try:
            n = n * 58 + _B58_INDEX[ch]
        except KeyError as exc:
            raise ValueError(f"invalid base58 char: {ch!r}") from exc
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + body


def decode_payload(text: str) -> Any:
    s = (text or "").strip()
    if not s:
        raise ValueError("订阅内容为空")
    if s[0] in "{[":
        return json.loads(s)
    try:
        raw = b58decode(s).decode("utf-8")
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        pass
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            padded = s + "=" * (-len(s) % 4)
            raw = decoder(padded).decode("utf-8")
            return json.loads(raw)
        except Exception:  
            continue
    raise ValueError("无法识别的订阅格式（支持 JSON / Base58 / Base64）")


def unwrap_proxy(api: str) -> str:
    api = (api or "").strip()
    match = re.search(r"[?&]url=([^&]+)", api)
    if match:
        inner = urllib.parse.unquote(match.group(1))
        if inner.startswith(("http://", "https://")):
            return inner
    return api


def source_id(api: str) -> str:
    return "s_" + hashlib.sha1(api.strip().rstrip("/").encode()).hexdigest()[:12]


def _entries(obj: Any) -> list[dict]:
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if not isinstance(obj, dict):
        return []
    if isinstance(obj.get("api_site"), dict):
        out = []
        for key, item in obj["api_site"].items():
            if isinstance(item, dict):
                row = dict(item)
                row.setdefault("_key", key)
                out.append(row)
        return out
    for field in ("sources", "sites"):
        if isinstance(obj.get(field), list):
            return [x for x in obj[field] if isinstance(x, dict)]
    return []


def normalize(obj: Any, origin: str = "subscription") -> tuple[list[dict], dict]:
    report = {"total": 0, "added": 0, "duplicated": 0, "skipped": 0,
              "adult": 0, "skipped_reasons": {}}
    seen: dict[str, dict] = {}

    def skip(reason: str) -> None:
        report["skipped"] += 1
        report["skipped_reasons"][reason] = report["skipped_reasons"].get(reason, 0) + 1

    for item in _entries(obj):
        report["total"] += 1
        raw_api = str(item.get("api") or item.get("url") or item.get("baseUrl") or "").strip()
        api = unwrap_proxy(raw_api)
        name = (str(item.get("name") or item.get("key") or item.get("_key") or "")
                .strip()[:60]) or "未命名源"
        marked = _ADULT_HINT in name or bool(item.get("is_adult") or item.get("adult"))
        if not api.startswith(("http://", "https://")):
            skip("接口地址无效")
            continue
        if not _CMS_HINT.search(api):
            skip("非苹果 CMS 接口")
            continue
        if item.get("type") not in (None, 0, 1, "0", "1"):
            skip("非采集站点类型")
            continue
        if item.get("disabled"):
            skip("订阅中已禁用")
            continue
        sid = source_id(api)
        if sid in seen:
            report["duplicated"] += 1
            continue
        seen[sid] = {
            "id": sid,
            "name": name,
            "key": str(item.get("_key") or urllib.parse.urlparse(api).hostname or "")[:80],
            "api": api,
            "detail": str(item.get("detail") or "").strip()[:200],
            "adult": marked,
            "enabled": False,
            "from": origin,
        }
        if marked:
            report["adult"] += 1
    report["added"] = len(seen)
    return list(seen.values()), report


def merge(remote: list[dict], local: list[dict]) -> tuple[list[dict], dict]:
    by_id = {s.get("id"): dict(s) for s in local if s.get("id")}
    added = updated = 0
    for item in remote:
        sid = item["id"]
        if sid in by_id:
            by_id[sid]["name"] = item["name"]
            by_id[sid]["api"] = item["api"]
            updated += 1
        else:
            by_id[sid] = dict(item)
            added += 1
    return list(by_id.values()), {"added": added, "updated": updated,
                                  "total": len(by_id)}


def merge_all(remote: list[dict], keep: list[dict]) -> tuple[list[dict], dict]:
    by_id = {s.get("id"): dict(s) for s in keep if s.get("id")}
    new_sources = 0
    for item in remote:
        if item["id"] not in by_id:
            new_sources += 1
        by_id[item["id"]] = dict(item)
    return list(by_id.values()), {"new_sources": new_sources,
                                  "sources_total": len(by_id)}


def _public_url(url: str) -> None:
    allow_intranet = bool(config.get().get("allow_intranet", True))
    netguard.validate_target(url, allow_intranet=allow_intranet,
                             label="订阅地址")


async def fetch(url: str, timeout: float = 5.0, max_bytes: int = 512 * 1024) -> str:
    current = str(url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                                 trust_env=False) as client:
        for _hop in range(3):
            _public_url(current)
            async with client.stream(
                    "GET", current,
                    headers={"User-Agent": "fnos_media_ext/0.1"}) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        break
                    current = urllib.parse.urljoin(current, location)
                    continue
                resp.raise_for_status()
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("订阅内容超过 512KB 上限")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                return raw.decode(resp.encoding or "utf-8", errors="replace")
    raise ValueError("订阅地址重定向次数过多")
