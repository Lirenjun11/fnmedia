#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT="/etc/systemd/system/fnmedia-ext.service"
mkdir -p "$ROOT/state" /run/fnmedia-ext

sed "s|@ROOT@|$ROOT|g" "$ROOT/deploy/fnmedia-ext.service.in" > "$UNIT"
systemctl daemon-reload
systemctl enable fnmedia-ext.service >/dev/null 2>&1 || true
systemctl restart fnmedia-ext.service
sleep 1
systemctl is-active --quiet fnmedia-ext.service
