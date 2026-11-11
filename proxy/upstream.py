#!/usr/bin/env python3
from __future__ import annotations

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length", "content-encoding", "access-control-allow-origin",
    "access-control-allow-credentials",
}
_SAFE = {"content-type", "content-range", "content-disposition", "accept-ranges",
         "cache-control", "etag", "last-modified", "location", "expires",
         "set-cookie", "pragma", "x-content-type-options"}


def make_client(uds: str = "/var/run/trim_media_upstream.sock",
                base_url: str = "") -> httpx.AsyncClient:
    if base_url:
        return httpx.AsyncClient(base_url=base_url,
                                 timeout=httpx.Timeout(30.0, connect=3.0),
                                 trust_env=False, follow_redirects=False)
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=uds), base_url="http://localhost",
        timeout=httpx.Timeout(30.0, connect=3.0),
        trust_env=False, follow_redirects=False,
    )


def _target(request: Request) -> str:
    path = request.url.path
    if request.url.query:
        path += "?" + request.url.query
    return path


def _headers(request: Request) -> dict:
    return {k: v for k, v in request.headers.items() if k.lower() not in _HOP}


def _out_headers(resp: httpx.Response) -> dict:
    out = {}
    for k, v in resp.headers.items():
        low = k.lower()
        if low in _SAFE:
            out[k] = v
    return out


async def forward(request: Request, client: httpx.AsyncClient) -> Response:
    body = await request.body()
    req = client.build_request(request.method, _target(request),
                               headers=_headers(request), content=body or None)
    resp = await client.send(req)
    return Response(content=resp.content, status_code=resp.status_code,
                    headers=_out_headers(resp),
                    media_type=resp.headers.get("content-type"))


async def stream(request: Request, client: httpx.AsyncClient) -> StreamingResponse:
    req = client.build_request(request.method, _target(request),
                               headers=_headers(request),
                               content=(await request.body()) or None)
    resp = await client.send(req, stream=True)
    headers = _out_headers(resp)

    async def body_iter():
        try:
            async for chunk in resp.aiter_bytes(65536):
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(body_iter(), status_code=resp.status_code,
                             headers=headers,
                             media_type=headers.get("content-type"))
