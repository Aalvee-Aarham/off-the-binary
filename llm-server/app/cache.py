"""Tiny in-memory LRU+TTL cache for deterministic (temperature=0) responses."""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from typing import Any


class ResponseCache:
    def __init__(self, max_items: int, ttl_s: float):
        self.max_items = max_items
        self.ttl_s = ttl_s
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    @staticmethod
    def key(*parts: Any) -> str:
        blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if item is None:
            return None
        ts, value = item
        if time.time() - ts > self.ttl_s:
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    def put(self, key: str, value: Any) -> None:
        if self.max_items <= 0:
            return
        self._data[key] = (time.time(), value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_items:
            self._data.popitem(last=False)

    def clear(self) -> int:
        n = len(self._data)
        self._data.clear()
        return n

    def __len__(self) -> int:
        return len(self._data)
