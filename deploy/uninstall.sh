#!/usr/bin/env bash
set -uo pipefail
systemctl stop fnmedia-ext.service >/dev/null 2>&1 || true
systemctl disable fnmedia-ext.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/fnmedia-ext.service
systemctl daemon-reload >/dev/null 2>&1 || true
rm -f /run/fnmedia-ext/fnmedia-ext.sock /run/fnmedia-ext/listen.sock
exit 0
