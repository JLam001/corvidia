"""Thread-safe health counters and gauges."""

from __future__ import annotations

import threading
from collections import Counter


class Health:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[str] = Counter()
        self._gauges: dict[str, object] = {}
        self._faults: dict[str, str] = {}

    def incr(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counters[name] += n

    def set(self, name: str, value: object) -> None:
        with self._lock:
            self._gauges[name] = value

    def fault(self, name: str, detail: str) -> None:
        with self._lock:
            self._faults[name] = detail

    def clear_fault(self, name: str) -> None:
        with self._lock:
            self._faults.pop(name, None)

    def count(self, name: str) -> int:
        with self._lock:
            return self._counters[name]

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "faults": dict(self._faults),
            }
