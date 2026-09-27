"""Bounded FIFO of candidates waiting for confirmation."""

from __future__ import annotations

import threading
from collections import OrderedDict

from .records import Candidate, CandidateKey


class ConfirmationQueue:
    """At most `max_pending` candidates, one per key, expiring after `expiry_s`.

    Shared by the detector thread (admit/remove) and the confirmation worker (pop).
    """

    def __init__(self, max_pending: int, expiry_s: float) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be >= 1")
        self._max = max_pending
        self._expiry_s = expiry_s
        self._items: OrderedDict[CandidateKey, Candidate] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, key: CandidateKey) -> bool:
        with self._lock:
            return key in self._items

    def has_room(self, key: CandidateKey) -> bool:
        """Cheap check before copying any pixels."""
        with self._lock:
            return key not in self._items and len(self._items) < self._max

    def admit(self, candidate: Candidate) -> bool:
        with self._lock:
            if candidate.key in self._items or len(self._items) >= self._max:
                return False
            self._items[candidate.key] = candidate
            return True

    def remove(self, key: CandidateKey) -> Candidate | None:
        with self._lock:
            return self._items.pop(key, None)

    def expire(self, now: float) -> list[Candidate]:
        with self._lock:
            expired = [c for c in self._items.values() if now - c.queued_mono >= self._expiry_s]
            for c in expired:
                del self._items[c.key]
            return expired

    def pop(self, now: float) -> tuple[Candidate | None, list[Candidate]]:
        """Return the oldest unexpired candidate and any entries that expired."""
        expired = self.expire(now)
        with self._lock:
            if not self._items:
                return None, expired
            _, candidate = self._items.popitem(last=False)
            return candidate, expired

    def drain(self) -> list[Candidate]:
        with self._lock:
            items = list(self._items.values())
            self._items.clear()
            return items

    def remove_epoch_other_than(self, source_epoch: int) -> list[Candidate]:
        with self._lock:
            stale = [c for c in self._items.values() if c.key.source_epoch != source_epoch]
            for c in stale:
                del self._items[c.key]
            return stale
