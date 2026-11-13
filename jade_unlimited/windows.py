"""Window, security-token, and input APIs. Never read game process memory."""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Callable

if os.name != "nt":
    raise RuntimeError("JadeUnlimited 的屏幕与鼠标操作目前仅支持 Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
shell32.IsUserAnAdmin.restype = wintypes.BOOL
shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
shell32.ShellExecuteW.restype = wintypes.HINSTANCE
user32.GetForegroundWindow.restype = wintypes.HWND
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short


def enable_dpi_awareness() -> None:
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except AttributeError:
        user32.SetProcessDPIAware()


@dataclass(frozen=True)
class Bounds:
    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True)
class Window:
    hwnd: int
    title: str


class InputNotSent(RuntimeError):
    """The failure happened before any mouse button input was inserted."""


def api_failure(function: str, code: int) -> str:
    detail = f"Windows 错误 {code}：{ctypes.FormatError(code).strip()}" if code else "Windows 未提供错误码"
    return f"{function} 失败（{detail}）"


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


def process_integrity(process: int) -> int:
    """Query only a process security token; never inspect game memory."""
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
        raise RuntimeError(api_failure("OpenProcessToken", ctypes.get_last_error()))
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 25, None, 0, ctypes.byref(needed))
        if needed.value == 0:
            raise RuntimeError(api_failure("GetTokenInformation", ctypes.get_last_error()))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, 25, buffer, needed.value, ctypes.byref(needed)):
            raise RuntimeError(api_failure("GetTokenInformation", ctypes.get_last_error()))
        label = ctypes.cast(buffer, ctypes.POINTER(SID_AND_ATTRIBUTES)).contents
        count = advapi32.GetSidSubAuthorityCount(label.Sid).contents.value
        return advapi32.GetSidSubAuthority(label.Sid, count - 1).contents.value
    finally:
        kernel32.CloseHandle(token)


def input_integrities(hwnd: int) -> tuple[int, int]:
    own_level = process_integrity(kernel32.GetCurrentProcess())
    pid = wintypes.DWORD()
    if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or not pid.value:
        raise RuntimeError("无法获取所选游戏的进程，请刷新窗口列表")
    process = kernel32.OpenProcess(0x1000, False, pid.value)
    if not process:
        raise RuntimeError(api_failure("OpenProcess(查询游戏权限)", ctypes.get_last_error()))
    try:
        return own_level, process_integrity(process)
    finally:
        kernel32.CloseHandle(process)


def ensure_input_access(hwnd: int) -> None:
    try:
        own_level, game_level = input_integrities(hwnd)
    except RuntimeError as exc:
        raise InputNotSent(f"无法检查游戏输入权限：{exc}。请刷新窗口列表，或以管理员身份重启助手") from exc
    if game_level > own_level:
        raise InputNotSent("游戏权限高于助手，Windows 会阻止模拟鼠标输入。请点击「以管理员重启」，确认 Windows 提示后重新填写数值。尚未发送任何鼠标点击")


def is_administrator() -> bool:
    return bool(shell32.IsUserAnAdmin())


def relaunch_as_administrator() -> None:
    if getattr(sys, "frozen", False):
        executable, arguments = sys.executable, []
        directory = str(Path(sys.executable).parent)
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        executable = str(pythonw if pythonw.exists() else Path(sys.executable))
        arguments = ["-m", "jade_unlimited"]
        directory = str(Path(__file__).resolve().parent.parent)
    ctypes.set_last_error(0)
    result = shell32.ShellExecuteW(None, "runas", executable, subprocess.list2cmdline(arguments), directory, 1)
    code = ctypes.get_last_error()
    if not result or result <= 32:
        if code == 1223:
            raise RuntimeError("已取消管理员重启，当前助手仍保持打开")
        raise RuntimeError(f"无法以管理员身份重启（返回 {result}，Windows 错误 {code}），也可右键 EXE 选择「以管理员身份运行」")


def client_bounds(hwnd: int) -> Bounds:
    if not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
        raise RuntimeError("目标窗口不存在或已最小化")
    rect = wintypes.RECT()
    point = wintypes.POINT(0, 0)
    for function, argument in ((user32.GetClientRect, rect), (user32.ClientToScreen, point)):
        ctypes.set_last_error(0)
        if not function(hwnd, ctypes.byref(argument)):
            raise RuntimeError(api_failure(function.__name__, ctypes.get_last_error()))
    return Bounds(point.x, point.y, rect.right - rect.left, rect.bottom - rect.top)


def list_windows() -> list[Window]:
    windows = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def collect(hwnd: int, _param: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        try:
            bounds = client_bounds(hwnd)
        except (OSError, RuntimeError):
            return True
        if bounds.width >= 800 and bounds.height >= 450:
            windows.append(Window(int(hwnd), title.value))
        return True

    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows(collect, 0)
    return sorted(windows, key=lambda window: ("星穹铁道" not in window.title, window.title))


def is_foreground(hwnd: int) -> bool:
    return user32.GetForegroundWindow() == hwnd and not user32.IsIconic(hwnd)


def cursor_at_corner() -> bool:
    point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        return True
    left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    right = left + user32.GetSystemMetrics(78) - 1
    bottom = top + user32.GetSystemMetrics(79) - 1
    corners = ((left, top), (left, bottom), (right, top), (right, bottom), (0, 0))
    return any(abs(point.x - x) <= 2 and abs(point.y - y) <= 2 for x, y in corners)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", wintypes.DWORD), ("data", INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


class Desktop:
    def __init__(self, hwnd: int):
        import mss
        self.hwnd = hwnd
        # mss owns thread-local GDI resources; create it in the worker thread.
        self.screen = mss.mss()

    def close(self) -> None:
        self.screen.close()

    def capture(self):
        import numpy as np
        bounds = client_bounds(self.hwnd)
        image = np.asarray(self.screen.grab({"left": bounds.left, "top": bounds.top, "width": bounds.width, "height": bounds.height}))[:, :, :3].copy()
        if client_bounds(self.hwnd) != bounds:
            raise RuntimeError("截屏时窗口位置发生变化，请保持窗口稳定")
        return image, bounds

    def prepare_click(self, point: tuple[int, int], expected: Bounds) -> None:
        ensure_input_access(self.hwnd)
        if not is_foreground(self.hwnd) or client_bounds(self.hwnd) != expected:
            raise InputNotSent("点击取消：游戏失去前台或窗口位置变化，未发送点击")
        if cursor_at_corner():
            raise InputNotSent("鼠标触及屏幕角落，已触发紧急停止，未发送点击")
        x, y = point
        if not (expected.left <= x < expected.left + expected.width and expected.top <= y < expected.top + expected.height):
            raise InputNotSent("点击坐标超出游戏客户区，未发送点击")
        ctypes.set_last_error(0)
        current = wintypes.POINT()
        already_there = user32.GetCursorPos(ctypes.byref(current)) and abs(current.x - x) <= 2 and abs(current.y - y) <= 2
        if not already_there and not user32.SetCursorPos(x, y):
            code = ctypes.get_last_error()
            raise InputNotSent(f"{api_failure('SetCursorPos(移动鼠标)', code)}。未发送鼠标点击；请检查游戏与助手权限是否一致")
        if not is_foreground(self.hwnd) or client_bounds(self.hwnd) != expected:
            raise InputNotSent("点击取消：移动鼠标后窗口状态发生变化，未发送点击")
        actual = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(actual)) or abs(actual.x - x) > 2 or abs(actual.y - y) > 2:
            raise InputNotSent("鼠标未到达指定位置，可能被游戏锁定，未发送点击")

    def click(self, point: tuple[int, int], expected: Bounds) -> None:
        try:
            self.prepare_click(point, expected)
        except InputNotSent:
            raise
        except Exception as exc:
            raise InputNotSent(f"点击前检查失败，未发送鼠标点击：{exc}") from exc
        down, up = INPUT(), INPUT()
        down.mi.dwFlags, up.mi.dwFlags = 0x0002, 0x0004
        ctypes.set_last_error(0)
        sent = user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
        code = ctypes.get_last_error()
        if sent == 0:
            raise InputNotSent(f"{api_failure('SendInput(按下左键)', code)}，返回 0/1。未发送鼠标点击；请以与游戏相同的权限运行助手")
        try:
            # Keep the button down across several game frames. Back-to-back
            # down/up in one batch can be missed by a game's input polling.
            time.sleep(0.04)
        finally:
            ctypes.set_last_error(0)
            released = user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
            code = ctypes.get_last_error()
            if released != 1:
                user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
                raise RuntimeError(f"{api_failure('SendInput(释放左键)', code)}；按下已发送并已尝试再次释放，请核对游戏数值后重新开始")


class KeyEdges:
    """Debounce held keys; F9 takes priority over a simultaneous F8."""

    def __init__(self, pressed: set[str]):
        self.pressed = set(pressed)

    def update(self, pressed: set[str]) -> str | None:
        newly_pressed = pressed - self.pressed
        self.pressed = set(pressed)
        if "stop" in newly_pressed:
            return "stop"
        if "toggle" in newly_pressed and "stop" not in pressed:
            return "toggle"
        return None


class Hotkeys:
    def __init__(self, callback: Callable[[str], None]):
        self.callback = callback
        self.closed = threading.Event()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="jade-hotkeys")

    def start(self) -> None:
        self.thread.start()
        self.ready.wait(2)

    def read_pressed(self) -> set[str]:
        # Only the current-down bit is reliable across other foreground apps.
        # No RegisterHotKey/WM_HOTKEY delivery or Tk focus is required.
        return {action for action, key in (("toggle", 0x77), ("stop", 0x78)) if user32.GetAsyncKeyState(key) & 0x8000}

    def _run(self) -> None:
        try:
            # Ignore any key already held when the app starts.
            edges = KeyEdges(self.read_pressed())
            self.ready.set()
            while not self.closed.wait(0.02):
                action = edges.update(self.read_pressed())
                if action:
                    self.callback(action)
        except Exception as exc:
            self.callback(f"error:热键检测失败：{exc}；可使用界面按钮开始或停止")
        finally:
            self.ready.set()

    def close(self) -> None:
        self.closed.set()
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=1)
