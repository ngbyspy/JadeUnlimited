"""Replay refreshes with a fake desktop. Never captures or controls the real game."""
from pathlib import Path
import json
from statistics import median
import sys
import time
from unittest.mock import patch

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jade_unlimited.engine import Settings, Worker
from jade_unlimited.windows import Bounds


class ReplayDesktop:
    def __init__(self):
        self.frames = [cv2.imdecode(np.fromfile(ROOT / "tests" / "fixtures" / f"{name}.png", dtype=np.uint8), cv2.IMREAD_COLOR) for name in ("health", "gold")]
        self.index = 0

    def capture(self):
        return self.frames[self.index].copy(), Bounds(0, 0, 1920, 1080)

    def click(self, _point, _bounds):
        time.sleep(0.04)  # Simulate the real mouse hold time.
        self.index = 1 - self.index


def main():
    desktop = ReplayDesktop()
    worker = Worker(Settings(123, 10, 0), lambda *_args: None)
    worker.active.set()
    timings = []
    with patch("jade_unlimited.engine.is_foreground", return_value=True):
        for _ in range(12):
            before = desktop.frames[desktop.index].copy()
            start = time.perf_counter()
            worker.perform(desktop, before, None)
            timings.append(time.perf_counter() - start)
    print(json.dumps({"mode": "offline replay, not game latency", "refreshes": worker.refreshes, "median_seconds": round(median(timings), 3), "p95_seconds": round(sorted(timings)[-1], 3), "refresh_min_wait": worker.settings.refresh_seconds, "purchase_min_wait": worker.settings.settle_seconds}, indent=2))


if __name__ == "__main__":
    main()
