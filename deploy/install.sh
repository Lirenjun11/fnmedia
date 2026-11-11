#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT="/etc/systemd/system/fnos-media-ext.service"
mkdir -p "$ROOT/state" /run/fnos-media-ext

sed "s|@ROOT@|$ROOT|g" "$ROOT/deploy/fnos-media-ext.service.in" > "$UNIT"
systemctl daemon-reload
systemctl enable fnos-media-ext.service >/dev/null 2>&1 || true
systemctl restart fnos-media-ext.service
sleep 1
systemctl is-active --quiet fnos-media-ext.service
