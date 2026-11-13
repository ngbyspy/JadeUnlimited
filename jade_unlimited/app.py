import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

from . import __version__
from .engine import Settings, Worker, data_directory
from .vision import Recognizer, annotate, normalize
from .windows import Desktop, Hotkeys, InputNotSent, enable_dpi_awareness, ensure_input_access, is_administrator, list_windows, relaunch_as_administrator


class Application:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(f"JadeUnlimited {__version__} · 黑市倒差价")
        root.geometry("850x800")
        root.minsize(800, 800)
        self.events = queue.Queue()
        self.worker: Worker | None = None
        self.windows = []
        self.preview_busy = False
        self.armed = False
        self.closing = False
        self.administrator = is_administrator()
        self.config_path = data_directory() / "preferences.json"
        self.status = tk.StringVar(value="请选择游戏窗口，先预览识别，再开始")
        self.stats = tk.StringVar(value="账本尚未开始")
        self.window_choice = tk.StringVar()
        self.health = tk.StringVar(value="")
        self.gold = tk.StringVar(value="")
        self.target = tk.StringVar(value="0")
        self.max_refreshes = tk.StringVar(value="500")
        self.settle = tk.StringVar(value="0.15")
        self.refresh_delay = tk.StringVar(value="0.08")
        self.threshold = tk.StringVar(value="0.88")
        self.load_preferences()
        self.build_ui()
        self.refresh_windows()
        self.hotkeys = Hotkeys(self.on_hotkey)
        self.hotkeys.start()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.drain_events)

    def build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="黑市自动倒差价", font=("Microsoft YaHei UI", 19, "bold")).pack(anchor="w")
        ttk.Label(outer, text="只买：5 金币换 10 生命值 / 10 生命值换 10 金币；刷新免费。", wraplength=770).pack(anchor="w", pady=(5, 14))
        window_row = ttk.Frame(outer)
        window_row.pack(fill="x")
        ttk.Label(window_row, text="游戏窗口").pack(side="left", padx=(0, 10))
        self.window_combo = ttk.Combobox(window_row, textvariable=self.window_choice, state="readonly")
        self.window_combo.pack(side="left", fill="x", expand=True)
        self.window_combo.bind("<<ComboboxSelected>>", lambda _event: self.invalidate_preview())
        self.refresh_button = ttk.Button(window_row, text="刷新列表", command=self.refresh_windows)
        self.refresh_button.pack(side="left", padx=(8, 0))

        config = ttk.LabelFrame(outer, text="本次开始前填写实际数值（暂停期间不要手动交易）", padding=12)
        config.pack(fill="x", pady=12)
        self.inputs = []
        for index, (label, variable, help_text) in enumerate([
            ("初始生命值", self.health, "1～100"),
            ("初始金币", self.gold, "≥0，必填"),
            ("目标金币", self.target, "0 = 不限"),
            ("刷新上限", self.max_refreshes, "0 = 不限"),
            ("刷新等待/秒", self.refresh_delay, "默认 0.08"),
            ("购买等待/秒", self.settle, "默认 0.15"),
            ("识别阈值", self.threshold, "默认 0.88"),
        ]):
            row, column = index // 2, (index % 2) * 3
            ttk.Label(config, text=label).grid(row=row, column=column, sticky="w", pady=5, padx=(0, 6))
            entry = ttk.Entry(config, textvariable=variable, width=10)
            entry.grid(row=row, column=column + 1, sticky="w", padx=(0, 8))
            ttk.Label(config, text=help_text, foreground="#666666").grid(row=row, column=column + 2, sticky="w", padx=(0, 18))
            self.inputs.append(entry)
        ttk.Label(outer, text="生命值 ≤10 禁止卖血；生命值 100 禁止回血；金币不足 5 禁止买回血。", wraplength=770).pack(anchor="w")
        ttk.Label(outer, text="仅对前台窗口点击；失焦自动暂停。鼠标移到屏幕角落可紧急停止。", wraplength=770).pack(anchor="w", pady=(4, 10))

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=4)
        self.preview_button = ttk.Button(actions, text="识别预览（不点击）", command=self.preview)
        self.preview_button.pack(side="left", padx=(0, 8))
        self.start_button = ttk.Button(actions, text="开始（等待切回游戏）", command=self.prepare_start)
        self.start_button.pack(side="left", padx=(0, 8))
        self.toggle_button = ttk.Button(actions, text="暂停 / 继续 F8", command=self.toggle, state="disabled")
        self.toggle_button.pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="停止 F9", command=self.stop).pack(side="left")

        status_box = ttk.LabelFrame(outer, text="运行状态", padding=10)
        status_box.pack(fill="x", pady=(12, 8))
        ttk.Label(status_box, textvariable=self.status, wraplength=735, foreground="#2459a6").pack(anchor="w")
        ttk.Label(status_box, textvariable=self.stats, wraplength=735).pack(anchor="w", pady=(6, 0))
        ttk.Label(status_box, text="账本值由初始输入和确认成交推算，不是实时 OCR 读取。", foreground="#666666").pack(anchor="w", pady=(6, 0))

        self.log_widget = scrolledtext.ScrolledText(outer, height=8, wrap="word", state="disabled", font=("Microsoft YaHei UI", 9))
        self.log_widget.pack(fill="both", expand=True, pady=(4, 8))
        footer = ttk.Frame(outer)
        footer.pack(fill="x")
        ttk.Button(footer, text="打开日志目录", command=lambda: os.startfile(data_directory())).pack(side="left")
        self.admin_button = ttk.Button(footer, text="管理员模式" if self.administrator else "以管理员重启", command=self.restart_as_administrator, state="disabled" if self.administrator else "normal")
        self.admin_button.pack(side="left", padx=(8, 0))
        ttk.Label(footer, text="建议游戏使用 16:9 窗口化 / 无边框窗口", foreground="#666666").pack(side="right")

    def emit(self, kind: str, payload: object) -> None:
        self.events.put((kind, payload))

    def log(self, text: str) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", text + "\n")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def load_preferences(self) -> None:
        try:
            preferences = json.loads(self.config_path.read_text(encoding="utf-8"))
            for key, variable in (("target_gold", self.target), ("max_refreshes", self.max_refreshes), ("threshold", self.threshold)):
                if key in preferences:
                    variable.set(str(preferences[key]))
            # Migrate the old shared 0.4–1s delay to separate fast defaults.
            if "refresh_seconds" in preferences:
                self.refresh_delay.set(str(preferences["refresh_seconds"]))
                self.settle.set(str(preferences.get("settle_seconds", 0.15)))
        except (OSError, ValueError, TypeError):
            pass

    def invalidate_preview(self) -> None:
        self.armed = False
        self.start_button.configure(text="开始（等待切回游戏）")

    def refresh_windows(self) -> None:
        previous = self.window_choice.get()
        self.windows = list_windows()
        labels = [f"{window.title}   [0x{window.hwnd:X}]" for window in self.windows]
        self.window_combo["values"] = labels
        self.window_choice.set(previous if previous in labels else labels[0] if labels else "")
        self.invalidate_preview()

    def selected_window(self):
        index = self.window_combo.current()
        if index < 0 or index >= len(self.windows):
            raise ValueError("请先打开游戏，并在列表中选择游戏窗口")
        return self.windows[index]

    def settings(self) -> Settings:
        window = self.selected_window()
        try:
            return Settings(window.hwnd, int(self.health.get()), int(self.gold.get()), int(self.target.get()), int(self.max_refreshes.get()), float(self.settle.get()), float(self.threshold.get()), float(self.refresh_delay.get()))
        except ValueError as exc:
            raise ValueError(f"请检查输入。生命值/金币/目标/次数须为整数。\n{exc}") from exc

    def set_session_controls(self, active: bool) -> None:
        for widget in self.inputs:
            widget.configure(state="disabled" if active else "normal")
        self.window_combo.configure(state="disabled" if active else "readonly")
        self.refresh_button.configure(state="disabled" if active else "normal")
        self.preview_button.configure(state="disabled" if active else "normal")
        self.start_button.configure(state="disabled" if active else "normal")
        self.toggle_button.configure(state="normal" if active else "disabled")
        self.admin_button.configure(state="disabled" if active or self.administrator else "normal")

    def restart_as_administrator(self) -> None:
        if self.worker and self.worker.is_alive():
            self.status.set("请先停止当前会话，再以管理员身份重启")
            return
        try:
            relaunch_as_administrator()
        except RuntimeError as exc:
            self.status.set(str(exc))
            self.log(str(exc))
            return
        self.close()

    def prepare_start(self) -> None:
        try:
            self.settings()
        except ValueError as exc:
            messagebox.showerror("输入不完整", str(exc), parent=self.root)
            return
        self.armed = True
        self.log("新会话开始：等待游戏回到前台。请确保输入是当前游戏的实际生命值和金币。")
        self.toggle()

    def toggle(self) -> None:
        if self.worker and self.worker.is_alive():
            if self.worker.active.is_set():
                self.worker.pause()
            else:
                self.worker.resume()
            return
        if not self.armed:
            self.status.set("请填写生命值和金币，点击「开始（等待切回游戏）」，再切回游戏")
            return
        try:
            settings = self.settings()
            ensure_input_access(settings.hwnd)
            Recognizer(settings.threshold)
            preferences = {key: value for key, value in asdict(settings).items() if key not in ("hwnd", "health", "gold")}
            self.config_path.write_text(json.dumps(preferences, indent=2), encoding="utf-8")
        except (ValueError, OSError, InputNotSent) as exc:
            self.armed = False
            self.status.set(str(exc))
            self.log(str(exc))
            messagebox.showerror("无法开始", str(exc), parent=self.root)
            return
        self.worker = Worker(settings, self.emit)
        self.set_session_controls(True)
        self.worker.resume()
        self.worker.start()
        self.armed = False

    def stop(self) -> None:
        self.armed = False
        self.start_button.configure(text="开始（等待切回游戏）")
        if self.worker and self.worker.is_alive():
            self.worker.request_stop()
            self.status.set("正在停止…")
        else:
            self.status.set("已停止")

    def on_hotkey(self, action: str) -> None:
        # F9 sets the event immediately, even if Tk is busy rendering a preview.
        if action == "stop" and self.worker:
            self.worker.request_stop()
        self.emit("hotkey", action)

    def preview(self) -> None:
        if self.preview_busy:
            return
        try:
            hwnd = self.selected_window().hwnd
            threshold = float(self.threshold.get())
            recognizer = Recognizer(threshold)
        except (ValueError, OSError) as exc:
            messagebox.showerror("无法预览", str(exc), parent=self.root)
            return
        self.preview_busy = True
        self.preview_button.configure(state="disabled")
        self.status.set("3 秒后截屏。请切回游戏并打开黑市；本次只识别，不点击。")

        def capture() -> None:
            desktop = None
            try:
                desktop = Desktop(hwnd)
                # Capture only a foreground game so an obscuring desktop window is not read.
                from .windows import is_foreground
                if not is_foreground(hwnd):
                    raise RuntimeError("游戏不在前台。请再按预览，并在 3 秒内切回游戏")
                raw, _ = desktop.capture()
                frame, _ = normalize(raw)
                scene = recognizer.read(frame)
                self.emit("preview", (frame, scene))
            except Exception as exc:
                self.emit("preview_error", str(exc))
            finally:
                if desktop:
                    desktop.close()

        self.root.after(3000, lambda: threading.Thread(target=capture, daemon=True, name="jade-preview").start())

    def show_preview(self, frame, scene) -> None:
        marked = annotate(frame, scene)[40:580, 390:1670]
        image = Image.fromarray(cv2.cvtColor(marked, cv2.COLOR_BGR2RGB))
        image.thumbnail((1160, 540))
        popup = tk.Toplevel(self.root)
        popup.title("识别预览 · 绿色框为目标商品 · 红点为刷新点击位置")
        photo = ImageTk.PhotoImage(image)
        label = ttk.Label(popup, image=photo)
        label.image = photo
        label.pack(padx=10, pady=10)
        descriptions = [f"第 {offer.slot + 1} 栏：{offer.product.value}（置信度 {offer.confidence:.3f}）" for offer in scene.offers]
        text = "\n".join(descriptions) or "本轮没有识别到目标商品"
        if not scene.shop_visible:
            text = "商店定位失败，禁止自动点击。请检查游戏分辨率、商店页面或截图是否黑屏。"
        ttk.Label(popup, text=text, wraplength=1100).pack(padx=12, pady=8)
        ttk.Button(popup, text="关闭", command=popup.destroy).pack(pady=(0, 12))
        self.log(text)
        self.status.set("预览完成。确认绿色框识别正确后，填写数值，点击开始并切回游戏。")

    def drain_events(self) -> None:
        if self.closing:
            return
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log(str(payload))
                elif kind == "status":
                    self.status.set(str(payload))
                elif kind == "stats":
                    self.stats.set(f"账本：生命 {payload['health']} / 100 · 金币 {payload['gold']} · 净增金币 {payload['profit']:+d}\n回血 {payload['health_bought']} 次 · 卖血 {payload['gold_bought']} 次 · 刷新 {payload['refreshes']} 次")
                elif kind == "done":
                    self.set_session_controls(False)
                    self.start_button.configure(text="开始（等待切回游戏）")
                    self.status.set(payload["reason"])
                    self.log(payload["reason"])
                    if not payload["uncertain"]:
                        self.log("本次会话已结束，账本仅包含已确认的交易。")
                    # Force a fresh, explicit ledger input for every new session.
                    self.health.set("")
                    self.gold.set("")
                elif kind == "hotkey":
                    if payload == "toggle":
                        self.log("收到 F8：切换暂停/继续")
                        self.toggle()
                    elif payload == "stop":
                        self.log("收到 F9：停止")
                        self.stop()
                    else:
                        self.log(str(payload))
                        self.status.set(str(payload))
                elif kind in ("preview", "preview_error"):
                    self.preview_busy = False
                    self.preview_button.configure(state="normal")
                    if kind == "preview":
                        self.show_preview(*payload)
                    else:
                        self.status.set(str(payload))
                        self.log(str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self.drain_events)

    def close(self) -> None:
        self.stop()
        self.closing = True
        self.hotkeys.close()
        self.root.withdraw()

        def finish() -> None:
            if self.worker and self.worker.is_alive():
                self.root.after(80, finish)
            else:
                self.root.destroy()
        finish()


def main() -> None:
    parser = argparse.ArgumentParser(description="JadeUnlimited 黑市倒差价助手")
    parser.add_argument("--analyze", type=Path, help="只分析本地截图，不操作鼠标")
    parser.add_argument("--output", type=Path, help="将标注后的截图保存到指定位置")
    args = parser.parse_args()
    if args.analyze:
        raw = cv2.imdecode(np.fromfile(args.analyze, dtype=np.uint8), cv2.IMREAD_COLOR)
        if raw is None:
            parser.error("截图无法读取")
        frame, _ = normalize(raw)
        scene = Recognizer().read(frame)
        print(json.dumps(asdict(scene), ensure_ascii=False, indent=2))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            cv2.imencode(".png", annotate(frame, scene))[1].tofile(args.output)
        return
    enable_dpi_awareness()
    root = tk.Tk()
    style = ttk.Style(root)
    style.theme_use("vista" if "vista" in style.theme_names() else "clam")
    root.option_add("*Font", ("Microsoft YaHei UI", 10))
    Application(root)
    root.mainloop()
