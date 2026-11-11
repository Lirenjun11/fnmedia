#!/usr/bin/env bash
set -uo pipefail
systemctl stop fnos-media-ext.service >/dev/null 2>&1 || true
systemctl disable fnos-media-ext.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/fnos-media-ext.service
systemctl daemon-reload >/dev/null 2>&1 || true
rm -f /run/fnos-media-ext/fnos-media-ext.sock /run/fnos-media-ext/listen.sock
exit 0
