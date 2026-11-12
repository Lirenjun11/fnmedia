#!/usr/bin/env python3
from __future__ import annotations

import os
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN_SOCK = Path(os.environ.get("FNOS_MEDIA_MAIN_SOCK", "/var/run/trim_media.sock"))
UPSTREAM_SOCK = Path(os.environ.get("FNOS_MEDIA_UPSTREAM_SOCK",
                                   "/var/run/trim_media_upstream.sock"))
RUN_DIR = Path("/run/fnmedia-ext")
STAGING = RUN_DIR / "listen.sock"
GATEWAY = RUN_DIR / "fnmedia-ext.sock"

_children: list[subprocess.Popen] = []
_staging_inode: int | None = None
_swapped = False


def health_ok(path: Path, timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(str(path))
            sock.sendall(b"GET /_ext/healthz HTTP/1.1\r\nHost: localhost\r\n"
                         b"Connection: close\r\n\r\n")
            data = sock.recv(4096)
            sock.close()
            if b"200" in data and b'"ok"' in data:
                return True
        except OSError:
            pass
        time.sleep(0.25)
    return False


def _socket_alive(path: Path) -> bool:
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(str(path))
        sock.sendall(b"GET / HTTP/1.0\r\n\r\n")
        data = sock.recv(64)
        sock.close()
        return bool(data)
    except OSError:
        return False


def _prepare() -> None:
    if UPSTREAM_SOCK.exists():
        if MAIN_SOCK.exists() and _socket_alive(MAIN_SOCK):
            UPSTREAM_SOCK.unlink()
        else:
            if MAIN_SOCK.exists():
                MAIN_SOCK.unlink()
            os.rename(UPSTREAM_SOCK, MAIN_SOCK)
    if not MAIN_SOCK.exists() or not stat.S_ISSOCK(MAIN_SOCK.stat().st_mode):
        raise RuntimeError("未检测到官方飞牛影视 socket，放弃接管")
    STAGING.parent.mkdir(parents=True, exist_ok=True)
    if STAGING.exists():
        STAGING.unlink()
    if GATEWAY.exists():
        GATEWAY.unlink()


def _spawn() -> None:
    env = dict(os.environ)
    env["FNOS_MEDIA_UPSTREAM_SOCK"] = str(UPSTREAM_SOCK)
    cmd = [sys.executable, "-m", "uvicorn", "app:app", "--app-dir", str(ROOT / "proxy"),
           "--uds", str(STAGING), "--no-access-log", "--log-level", "warning"]
    _children.append(subprocess.Popen(cmd, cwd=str(ROOT / "proxy"), env=env))
    gateway = subprocess.Popen(
        [sys.executable, str(ROOT / "proxy" / "webui_gateway.py"),
         str(ROOT), str(MAIN_SOCK)], cwd=str(ROOT), env=env)
    _children.append(gateway)


def _swap() -> None:
    global _staging_inode, _swapped
    _staging_inode = STAGING.stat().st_ino
    os.chmod(STAGING, 0o666)
    os.rename(MAIN_SOCK, UPSTREAM_SOCK)
    os.rename(STAGING, MAIN_SOCK)
    _swapped = True


def _rollback() -> None:
    if not _swapped:
        return
    try:
        if MAIN_SOCK.exists() and MAIN_SOCK.stat().st_ino == _staging_inode:
            MAIN_SOCK.unlink()
    except OSError:
        pass
    try:
        if UPSTREAM_SOCK.exists() and not MAIN_SOCK.exists():
            os.rename(UPSTREAM_SOCK, MAIN_SOCK)
    except OSError:
        pass


def _shutdown(*_args) -> None:
    for child in _children:
        try:
            child.terminate()
        except OSError:
            pass
    for child in _children:
        try:
            child.wait(timeout=8)
        except Exception:  
            try:
                child.kill()
            except OSError:
                pass
    _rollback()
    sys.exit(0)


def main() -> int:
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    _prepare()
    _spawn()
    if not health_ok(STAGING):
        _shutdown()
        return 1
    _swap()
    print("[fnmedia-ext] takeover active", flush=True)
    while True:
        if any(child.poll() is not None for child in _children):
            print("[fnmedia-ext] child exited, rolling back", flush=True)
            _shutdown()
            return 1
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
