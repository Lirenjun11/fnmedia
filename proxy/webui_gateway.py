#!/usr/bin/env python3
from __future__ import annotations

import os
import socket
import sys
import threading
from pathlib import Path

APP_PREFIX = "/app/fnmedia-ext"
SOCK_NAME = "fnmedia-ext.sock"
MAX_HEADER = 1 << 20
MAX_BODY = 8 << 20
RELAY_TIMEOUT = 60


def gateway_socket_path(base: Path) -> Path:
    parent = base.resolve().parent
    if (parent / "ui").is_dir() and base.name == "repo":
        return parent / SOCK_NAME
    run = Path("/run/fnmedia-ext")
    run.mkdir(parents=True, exist_ok=True)
    return run / SOCK_NAME


def _rewrite(head: bytes) -> bytes:
    lines = head.split(b"\r\n")
    parts = lines[0].split()
    if len(parts) >= 3:
        target = parts[1].decode("latin-1")
        path = target.split("?", 1)[0]
        if path in (APP_PREFIX, APP_PREFIX + "/", "/", ""):
            target = "/v/online-admin"
        elif path.startswith(APP_PREFIX + "/"):
            target = target[len(APP_PREFIX):]
            if not target.startswith("/"):
                target = "/" + target
        lines[0] = b" ".join([parts[0], target.encode("latin-1"), parts[2]])
    kept = [lines[0]] + [line for line in lines[1:]
                         if line.split(b":", 1)[0].strip().lower() != b"expect"]
    return b"\r\n".join(kept) + b"\r\n\r\n"


def _recv_headers(client: socket.socket) -> tuple[bytes, bytes] | None:
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > MAX_HEADER:
            return None
        chunk = client.recv(8192)
        if not chunk:
            return None
        data += chunk
    return tuple(data.split(b"\r\n\r\n", 1))  


def _content_length(head: bytes) -> int:
    for line in head.split(b"\r\n")[1:]:
        if b":" in line:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                try:
                    return int(value.strip())
                except ValueError:
                    return -1
    return 0


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle(client: socket.socket, upstream: Path) -> None:
    remote = None
    try:
        received = _recv_headers(client)
        if received is None:
            return
        head, partial = received
        length = _content_length(head)
        if length < 0 or length > MAX_BODY:
            return
        body = partial
        while len(body) < length:
            chunk = client.recv(min(65536, length - len(body)))
            if not chunk:
                return
            body += chunk
        remote = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        remote.settimeout(RELAY_TIMEOUT)
        remote.connect(str(upstream))
        remote.sendall(_rewrite(head) + body[:length])
        a = threading.Thread(target=_pump, args=(client, remote), daemon=True)
        b = threading.Thread(target=_pump, args=(remote, client), daemon=True)
        a.start()
        b.start()
        a.join()
        b.join()
    except OSError:
        pass
    finally:
        for s in (client, remote):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    repo = Path(args[0]) if args else Path(__file__).resolve().parent.parent
    upstream = Path(args[1]) if len(args) > 1 else Path("/var/run/trim_media.sock")
    path = gateway_socket_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o666)
    server.listen(64)
    while True:
        client, _ = server.accept()
        threading.Thread(target=_handle, args=(client, upstream), daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
