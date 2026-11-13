from dataclasses import asdict, dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Callable

import cv2
import numpy as np

from .model import Account, Offer, choose_offer
from .vision import REFRESH, SLOTS, Recognizer, Scene, Viewport, annotate, content_difference, normalize
from .windows import Bounds, Desktop, InputNotSent, cursor_at_corner, is_foreground


def data_directory() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "JadeUnlimited"
    root.mkdir(parents=True, exist_ok=True)
    return root


@dataclass(frozen=True)
class Settings:
    hwnd: int
    health: int
    gold: int
    target_gold: int = 0
    max_refreshes: int = 500
    settle_seconds: float = 0.15
    threshold: float = 0.88
    refresh_seconds: float = 0.08

    def __post_init__(self) -> None:
        Account(self.health, self.gold)
        if self.hwnd <= 0:
            raise ValueError("请选择游戏窗口")
        if self.target_gold < 0 or self.max_refreshes < 0:
            raise ValueError("目标金币和刷新上限不能为负数；0 表示不限")
        for name, value in (("购买等待", self.settle_seconds), ("刷新等待", self.refresh_seconds)):
            if not math.isfinite(value) or not 0.02 <= value <= 10:
                raise ValueError(f"{name}必须在 0.02～10 秒之间")
        if not 0.80 <= self.threshold <= 0.99:
            raise ValueError("识别阈值必须在 0.80～0.99 之间")


class Stopped(Exception):
    pass


class Uncertain(RuntimeError):
    """A click may have reached the game. Never automatically retry it."""


class Worker(threading.Thread):
    def __init__(self, settings: Settings, emit: Callable[[str, object], None], desktop_factory=Desktop):
        super().__init__(daemon=True, name="jade-worker")
        self.settings = settings
        self.emit = emit
        self.desktop_factory = desktop_factory
        self.account = Account(settings.health, settings.gold)
        self.recognizer = Recognizer(settings.threshold)
        self.stop_event = threading.Event()
        self.active = threading.Event()
        self.waiting_for_foreground = threading.Event()
        self.refreshes = 0
        self.pending = False
        self.last_frame: np.ndarray | None = None
        self.journal = None
        self.ready_scene = None
        self.verification_frames = 0

    def resume(self) -> None:
        self.ready_scene = None
        self.waiting_for_foreground.set()
        self.active.set()
        self.emit("status", "已请求开始/继续，等待切回所选游戏窗口；F8 暂停，F9 停止")

    def pause(self, reason: str = "已暂停，切回游戏后按 F8 继续") -> None:
        self.ready_scene = None
        self.active.clear()
        self.waiting_for_foreground.clear()
        self.emit("status", reason)

    def request_stop(self) -> None:
        self.stop_event.set()

    def wait(self, seconds: float) -> None:
        if self.stop_event.wait(seconds):
            raise Stopped()

    def record(self, event: str, **fields) -> None:
        payload = {"time": datetime.now().astimezone().isoformat(), "event": event, **fields}
        if self.journal:
            self.journal.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.journal.flush()

    def stats(self) -> None:
        self.emit("stats", {**asdict(self.account), "refreshes": self.refreshes, "profit": self.account.gold - self.settings.gold})

    def snapshot(self, desktop: Desktop) -> tuple[np.ndarray, Scene, Bounds, Viewport]:
        if not is_foreground(self.settings.hwnd):
            raise RuntimeError("游戏失去前台；请切回游戏")
        raw, bounds = desktop.capture()
        frame, viewport = normalize(raw)
        self.last_frame = frame
        return frame, self.recognizer.read(frame), bounds, viewport

    def stable_scene(self, desktop: Desktop):
        previous = None
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            frame, scene, bounds, viewport = self.snapshot(desktop)
            if scene.shop_visible and previous is not None and content_difference(previous, frame) < 2.5:
                return frame, scene, bounds, viewport
            previous = frame if scene.shop_visible else None
            self.wait(0.04)
        raise RuntimeError("未找到稳定的黑市商店画面，请先用「识别预览」检查")

    def verify(self, desktop: Desktop, before: np.ndarray, offer: Offer | None):
        self.wait(self.settings.settle_seconds if offer else self.settings.refresh_seconds)
        deadline = time.monotonic() + 4.0
        previous = None
        self.verification_frames = 0
        while time.monotonic() < deadline:
            try:
                frame, scene, bounds, viewport = self.snapshot(desktop)
                self.verification_frames += 1
            except RuntimeError as exc:
                raise Uncertain(f"点击后无法确认结果：{exc}") from exc
            if offer is None:
                changed = all(scene.ready_slots) and content_difference(before, frame) >= 3.0
            else:
                still_present = any(item.slot == offer.slot and item.product == offer.product for item in scene.offers)
                # A small hover effect must not count as a purchase.
                changed = not still_present and content_difference(before, frame, offer.slot) >= 3.0
            stable = previous is not None and content_difference(previous, frame) < 2.5
            if scene.shop_visible and changed and stable:
                return frame, scene, bounds, viewport
            # Only compare consecutive visible, changed observations. This keeps
            # a blank fade, old lineup, or disappearing title from confirming.
            previous = frame if scene.shop_visible and changed else None
            self.wait(0.04)
        action = f"第 {offer.slot + 1} 栏商品成交" if offer else "刷新成功"
        raise Uncertain(f"未能确认{action}。不重试本次点击，请核对生命值/金币后重新开始")

    def preflight(self, desktop: Desktop, baseline: np.ndarray, offer: Offer | None):
        if self.stop_event.is_set():
            raise Stopped()
        if not self.active.is_set():
            return None
        frame, scene, bounds, viewport = self.snapshot(desktop)
        if not scene.shop_visible or content_difference(baseline, frame) >= 2.5:
            return None
        if offer is None and choose_offer(self.account, scene.offers) is not None:
            # A target can become readable during the last few fade frames.
            # Reconsider it instead of refreshing past it.
            return None
        if offer and not any(item.slot == offer.slot and item.product == offer.product for item in scene.offers):
            return None
        return frame, bounds, viewport

    def perform(self, desktop: Desktop, before: np.ndarray, offer: Offer | None) -> bool:
        start = time.perf_counter()
        checked = self.preflight(desktop, before, offer)
        if checked is None:
            return False
        frame, bounds, viewport = checked
        preflight_done = time.perf_counter()
        point = (SLOTS[offer.slot].center[0], SLOTS[offer.slot].y + 112) if offer else REFRESH.center
        relative = viewport.screen_point(point)
        screen_point = (bounds.left + relative[0], bounds.top + relative[1])
        if self.stop_event.is_set() or not self.active.is_set():
            return False
        # Once attempted, a click is never retried without human reconciliation.
        self.pending = True
        self.record("click_intent", slot=offer.slot + 1 if offer else None, product=offer.product.value if offer else "refresh", point=screen_point)
        try:
            desktop.click(screen_point, bounds)
            click_done = time.perf_counter()
            verified = self.verify(desktop, frame, offer)
        except InputNotSent as exc:
            self.pending = False
            self.record("input_rejected", reason=str(exc), mouse_buttons_sent=False)
            raise
        except Stopped:
            raise
        except Exception as exc:
            raise Uncertain(str(exc)) from exc
        self.pending = False
        self.ready_scene = verified
        finished = time.perf_counter()
        timing = {"preflight": round((preflight_done - start) * 1000, 1), "mouse": round((click_done - preflight_done) * 1000, 1), "confirmation": round((finished - click_done) * 1000, 1), "total": round((finished - start) * 1000, 1)}
        timing_text = f" · 本轮 {timing['total'] / 1000:.2f}s（鼠标 {timing['mouse']:.0f}ms / 确认 {timing['confirmation']:.0f}ms）"
        if offer:
            self.account.commit(offer.product)
            self.emit("log", f"第 {offer.slot + 1} 栏成交：{offer.product.value}；生命 {self.account.health}，金币 {self.account.gold}" + timing_text)
        else:
            self.refreshes += 1
            self.emit("log", f"已确认第 {self.refreshes} 次刷新" + timing_text)
        self.record("confirmed", action=offer.product.value if offer else "refresh", account=asdict(self.account), refreshes=self.refreshes, timing_ms=timing, verification_frames=self.verification_frames)
        self.stats()
        return True

    def save_diagnostic(self) -> str:
        if self.last_frame is None:
            return ""
        try:
            directory = data_directory() / "diagnostics"
            directory.mkdir(exist_ok=True)
            path = directory / f"{datetime.now():%Y%m%d-%H%M%S-%f}.png"
            marked = annotate(self.last_frame, self.recognizer.read(self.last_frame))
            cv2.imencode(".png", marked[40:580, 390:1670])[1].tofile(path)
            return str(path)
        except Exception:
            return ""

    def run(self) -> None:
        desktop = None
        reason = "已停止"
        try:
            directory = data_directory() / "logs"
            directory.mkdir(exist_ok=True)
            path = directory / f"session-{datetime.now():%Y%m%d-%H%M%S-%f}.jsonl"
            self.journal = path.open("w", encoding="utf-8")
            self.record("start", settings=asdict(self.settings))
            self.emit("log", f"日志：{path}")
            self.stats()
            desktop = self.desktop_factory(self.settings.hwnd)
            while not self.stop_event.is_set():
                if not self.active.wait(0.1):
                    continue
                if cursor_at_corner():
                    reason = "鼠标触及屏幕角落，已停止"
                    break
                if not is_foreground(self.settings.hwnd):
                    if self.waiting_for_foreground.is_set():
                        # An explicit start/resume outside the game waits instead
                        # of immediately consuming the request by auto-pausing.
                        self.wait(0.1)
                    else:
                        self.pause("已暂停：游戏不在前台。按 F8 或点继续，再切回游戏")
                    continue
                if self.waiting_for_foreground.is_set():
                    self.waiting_for_foreground.clear()
                    self.emit("log", "所选游戏已回到前台，开始/继续运行")
                self.emit("status", "运行中 · F8 暂停 / F9 停止")
                try:
                    ready = self.ready_scene
                    self.ready_scene = None
                    frame, scene, _, _ = ready if ready is not None else self.stable_scene(desktop)
                    if self.settings.target_gold and self.account.gold >= self.settings.target_gold:
                        reason = "已达到目标金币"
                        break
                    offer = choose_offer(self.account, scene.offers)
                    if offer:
                        self.perform(desktop, frame, offer)
                    elif self.account.bankrupt:
                        reason = "生命值 ≤10 且金币不足 5，无法继续交易"
                        break
                    elif self.settings.max_refreshes and self.refreshes >= self.settings.max_refreshes:
                        reason = "已达到本次刷新上限"
                        break
                    else:
                        self.perform(desktop, frame, None)
                except (Uncertain, InputNotSent):
                    raise
                except RuntimeError as exc:
                    self.pause(f"已暂停：{exc}")
                    self.emit("log", str(exc))
        except Stopped:
            reason = "已停止"
        except Exception as exc:
            reason = f"异常停止：{exc}"
            diagnostic = self.save_diagnostic()
            if diagnostic:
                self.emit("log", f"异常画面（仅商店区域）：{diagnostic}")
        finally:
            if self.pending:
                reason += "；有一次点击结果未确认，重新开始前必须核对游戏生命值和金币"
            if desktop:
                try:
                    desktop.close()
                except Exception as exc:
                    self.emit("log", f"关闭截屏资源时发生错误：{exc}")
            self.record("end", reason=reason, uncertain=self.pending, account=asdict(self.account))
            if self.journal:
                self.journal.close()
            self.emit("done", {"reason": reason, "uncertain": self.pending})
