#!/usr/bin/env python3
from __future__ import annotations

import time
from collections import deque

LOGS: deque = deque(maxlen=200)
STARTED_AT = time.time()


def log(message: str) -> None:
    LOGS.append({"ts": round(time.time(), 3), "msg": message[:300]})


def recent(limit: int = 100) -> list[dict]:
    return list(LOGS)[-max(1, min(limit, 200)):]
