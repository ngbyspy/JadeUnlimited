import ctypes
from ctypes import wintypes
from contextlib import ExitStack
import sys
import unittest
from unittest.mock import patch

from jade_unlimited.windows import Bounds, Desktop, INPUT, InputNotSent, ensure_input_access, relaunch_as_administrator


class WindowsInputTests(unittest.TestCase):
    def setUp(self):
        self.desktop = Desktop.__new__(Desktop)
        self.desktop.hwnd = 123
        self.bounds = Bounds(0, 0, 1920, 1080)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("jade_unlimited.windows.ensure_input_access"))
        self.stack.enter_context(patch("jade_unlimited.windows.is_foreground", return_value=True))
        self.stack.enter_context(patch("jade_unlimited.windows.client_bounds", return_value=self.bounds))
        self.stack.enter_context(patch("jade_unlimited.windows.cursor_at_corner", return_value=False))
        self.cursor = (800, 600)
        def move_cursor(x, y):
            if self.move.return_value:
                self.cursor = (x, y)
            return self.move.return_value
        self.move = self.stack.enter_context(patch("jade_unlimited.windows.user32.SetCursorPos", return_value=True, side_effect=move_cursor))
        self.send = self.stack.enter_context(patch("jade_unlimited.windows.user32.SendInput", return_value=1))
        self.sleep = self.stack.enter_context(patch("jade_unlimited.windows.time.sleep"))

        def read_position(pointer):
            point = ctypes.cast(pointer, ctypes.POINTER(wintypes.POINT)).contents
            point.x, point.y = self.cursor
            return True

        self.stack.enter_context(patch("jade_unlimited.windows.user32.GetCursorPos", side_effect=read_position))

    def test_move_fails_with_error_zero_is_explicitly_not_a_click(self):
        self.move.return_value = False
        ctypes.set_last_error(0)
        with self.assertRaises(InputNotSent) as caught:
            self.desktop.click((901, 513), self.bounds)
        self.assertIn("SetCursorPos", str(caught.exception))
        self.assertIn("Windows 未提供错误码", str(caught.exception))
        self.assertNotIn("操作成功", str(caught.exception))
        self.send.assert_not_called()

    def test_zero_events_sent_is_not_uncertain_and_is_not_retried(self):
        self.send.return_value = 0
        with self.assertRaises(InputNotSent) as caught:
            self.desktop.click((901, 513), self.bounds)
        self.assertIn("SendInput", str(caught.exception))
        self.assertIn("0/1", str(caught.exception))
        self.send.assert_called_once()

    def test_partial_input_attempts_release_and_remains_uncertain(self):
        self.send.side_effect = [1, 0, 1]
        with self.assertRaises(RuntimeError) as caught:
            self.desktop.click((901, 513), self.bounds)
        self.assertNotIsInstance(caught.exception, InputNotSent)
        self.assertIn("释放左键", str(caught.exception))
        self.assertEqual(self.send.call_count, 3)
        release = self.send.call_args.args[1]
        self.assertEqual(ctypes.cast(release, ctypes.POINTER(INPUT)).contents.mi.dwFlags, 0x0004)

    def test_success_uses_correct_windows_input_structure(self):
        self.desktop.click((901, 513), self.bounds)
        self.assertEqual(self.send.call_count, 2)
        flags = []
        for call in self.send.call_args_list:
            count, pointer, size = call.args
            self.assertEqual(count, 1)
            self.assertEqual(size, 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
            flags.append(ctypes.cast(pointer, ctypes.POINTER(INPUT)).contents.mi.dwFlags)
        self.assertEqual(flags, [0x0002, 0x0004])
        self.sleep.assert_called_once_with(0.04)

    def test_cursor_not_arriving_prevents_button_input(self):
        self.move.side_effect = None
        with self.assertRaises(InputNotSent):
            self.desktop.click((1000, 600), self.bounds)
        self.send.assert_not_called()

    def test_repeated_refresh_does_not_reposition_pointer_already_at_button(self):
        self.cursor = (901, 513)
        self.desktop.click((901, 513), self.bounds)
        self.move.assert_not_called()

    def test_interruption_during_hold_still_releases_button(self):
        self.sleep.side_effect = RuntimeError("interrupted")
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.desktop.click((901, 513), self.bounds)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(ctypes.cast(self.send.call_args.args[1], ctypes.POINTER(INPUT)).contents.mi.dwFlags, 0x0004)

    def test_pre_input_window_failure_is_not_a_possible_purchase(self):
        with patch("jade_unlimited.windows.client_bounds", side_effect=RuntimeError("window disappeared")):
            with self.assertRaises(InputNotSent):
                self.desktop.click((901, 513), self.bounds)
        self.move.assert_not_called()
        self.send.assert_not_called()


class PermissionTests(unittest.TestCase):
    def test_higher_integrity_game_is_rejected_before_input(self):
        with patch("jade_unlimited.windows.input_integrities", return_value=(0x2000, 0x3000)):
            with self.assertRaises(InputNotSent) as caught:
                ensure_input_access(123)
        self.assertIn("以管理员重启", str(caught.exception))

    def test_equal_or_lower_integrity_game_is_allowed(self):
        for levels in ((0x2000, 0x2000), (0x3000, 0x3000), (0x3000, 0x2000)):
            with self.subTest(levels=levels), patch("jade_unlimited.windows.input_integrities", return_value=levels):
                ensure_input_access(123)

    def test_permission_query_failure_prevents_input(self):
        with patch("jade_unlimited.windows.input_integrities", side_effect=RuntimeError("access denied")):
            with self.assertRaises(InputNotSent):
                ensure_input_access(123)

    def test_administrator_restart_requests_windows_consent(self):
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", r"C:\Example Folder\JadeUnlimited.exe"), patch("jade_unlimited.windows.shell32.ShellExecuteW", return_value=33) as execute:
            relaunch_as_administrator()
        self.assertEqual(execute.call_args.args[1], "runas")
        self.assertEqual(execute.call_args.args[2], r"C:\Example Folder\JadeUnlimited.exe")

    def test_cancelled_windows_consent_has_clear_message(self):
        def cancelled(*_args):
            ctypes.set_last_error(1223)
            return 5
        with patch("jade_unlimited.windows.shell32.ShellExecuteW", side_effect=cancelled):
            with self.assertRaisesRegex(RuntimeError, "已取消管理员重启"):
                relaunch_as_administrator()


if __name__ == "__main__":
    unittest.main()
