#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
PY="${FNM_PYTHON:-python3}"

if [ ! -x "$VENV/bin/python" ]; then
    "$PY" -m venv "$VENV"
fi

if "$VENV/bin/python" -c 'import fastapi, uvicorn, httpx' >/dev/null 2>&1; then
    exit 0
fi

if [ -d "$ROOT/wheelhouse" ]; then
    if "$VENV/bin/pip" install --no-index --find-links="$ROOT/wheelhouse" \
        -r "$ROOT/requirements.txt" >/dev/null 2>&1; then
        exit 0
    fi
fi

for INDEX in \
    "https://mirrors.aliyun.com/pypi/simple/" \
    "https://pypi.tuna.tsinghua.edu.cn/simple/" \
    "https://pypi.org/simple/"; do
    if "$VENV/bin/pip" install -i "$INDEX" --trusted-host "$(echo "$INDEX" | sed -E 's#https?://([^/]+)/.*#\1#')" \
        -r "$ROOT/requirements.txt" >/dev/null 2>&1; then
        exit 0
    fi
done

echo "ERROR: Python 依赖安装失败（离线包与在线镜像均不可用）" >&2
exit 1
