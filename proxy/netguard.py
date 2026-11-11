"""共享的 URL 目标校验：订阅、海报、HLS 分片与直链统一走这里。

公网地址始终允许；局域网 / 回环地址由 allow_intranet 控制；链路本地、
保留、组播与未指定地址始终拦截，避免误触云元数据等敏感端点。
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse

_BLOCKED_HOSTS = {"metadata.google.internal", "metadata"}


def _addresses(host: str, port: int) -> list:
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def validate_target(url: str, *, allow_intranet: bool,
                   label: str = "地址") -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"{label}必须是 http(s) URL")
    host = parsed.hostname.strip("[]").lower()
    if host in _BLOCKED_HOSTS:
        raise ValueError(f"不允许访问 {host}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = _addresses(host, port)
    except OSError as exc:
        raise ValueError(f"{label}域名无法解析: {host}") from exc
    for addr in addresses:
        if addr.is_link_local or addr.is_multicast or addr.is_unspecified:
            raise ValueError(f"{label}不允许访问链路本地或保留地址")
        # IPv6 回环（::1）同时被标记为 reserved，需先按回环处理
        if addr.is_loopback:
            if not allow_intranet:
                raise ValueError(f"{label}不允许访问内网地址")
            continue
        if addr.is_private:
            if addr.is_reserved or not allow_intranet:
                raise ValueError(f"{label}不允许访问内网地址")
            continue
        if addr.is_reserved:
            raise ValueError(f"{label}不允许访问链路本地或保留地址")
    return url
