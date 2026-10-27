"""Small transfer progress / rate helper shared by sender and receiver."""

from __future__ import annotations

import time


class ProgressTracker:
    """Progress values are mutated while the owning service lock is held."""

    def __init__(self) -> None:
        self.reset(0, 0, 0)

    def reset(self, total: int, base: int, files_total: int) -> None:
        self.total = max(0, int(total))
        self.base = max(0, min(int(base), self.total))
        self.files_total = int(files_total)
        self.new_bytes = 0
        self.started_at = time.monotonic()
        self.finished_at: float | None = None

    def add(self, amount: int) -> None:
        self.new_bytes += max(0, int(amount))

    def finish(self) -> None:
        self.new_bytes = max(0, self.total - self.base)
        self.finished_at = time.monotonic()

    def snapshot(self, **extra: object) -> dict[str, object]:
        now = self.finished_at or time.monotonic()
        elapsed = max(0.0, now - self.started_at)
        done = min(self.total, self.base + self.new_bytes)
        if self.finished_at is not None:
            done = self.total
        speed = self.new_bytes / elapsed if elapsed > 0.05 else 0.0
        remaining = max(0, self.total - done)
        eta = remaining / speed if speed > 0 and remaining else 0.0
        percent = 100.0 if self.total == 0 and self.finished_at else (done / self.total * 100.0 if self.total else 0.0)
        payload: dict[str, object] = {
            "done": int(done),
            "total": int(self.total),
            "base": int(self.base),
            "new_bytes": int(self.new_bytes),
            "new_total": int(max(0, self.total - self.base)),
            "percent": round(min(100.0, max(0.0, percent)), 2),
            "speed": round(speed, 1),
            "eta": int(round(eta)),
            "elapsed": round(elapsed, 1),
            "files_total": self.files_total,
        }
        payload.update(extra)
        return payload
