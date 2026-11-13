import unittest
from unittest.mock import Mock, patch

from jade_unlimited.windows import Hotkeys, KeyEdges


class HotkeyTests(unittest.TestCase):
    def test_hold_does_not_repeat_and_startup_held_key_is_ignored(self):
        edges = KeyEdges({"toggle"})
        self.assertIsNone(edges.update({"toggle"}))
        self.assertIsNone(edges.update(set()))
        self.assertEqual(edges.update({"toggle"}), "toggle")
        self.assertIsNone(edges.update({"toggle"}))
        self.assertIsNone(edges.update(set()))
        self.assertEqual(edges.update({"toggle"}), "toggle")

    def test_stop_takes_priority_and_blocks_toggle_while_held(self):
        edges = KeyEdges(set())
        self.assertEqual(edges.update({"toggle", "stop"}), "stop")
        self.assertIsNone(edges.update({"stop"}))
        self.assertIsNone(edges.update({"toggle", "stop"}))
        self.assertIsNone(edges.update({"toggle"}))
        self.assertIsNone(edges.update(set()))
        self.assertEqual(edges.update({"toggle"}), "toggle")

    def test_polling_uses_current_key_state_without_foreground_messages(self):
        callback = Mock()
        hotkeys = Hotkeys(callback)
        with patch("jade_unlimited.windows.user32.GetAsyncKeyState", side_effect=lambda key: -32768 if key == 0x77 else 1), patch("jade_unlimited.windows.user32.GetForegroundWindow", side_effect=AssertionError("must not depend on foreground")):
            self.assertEqual(hotkeys.read_pressed(), {"toggle"})

    def test_background_listener_delivers_once_per_press_without_message_loop(self):
        callback = Mock()
        hotkeys = Hotkeys(callback)
        hotkeys.read_pressed = Mock(side_effect=[set(), {"toggle"}, {"toggle"}, set(), {"toggle"}, {"stop"}])
        hotkeys.closed = Mock()
        hotkeys.closed.wait.side_effect = [False, False, False, False, False, True]
        hotkeys._run()
        self.assertEqual([call.args[0] for call in callback.call_args_list], ["toggle", "toggle", "stop"])

    def test_listener_thread_closes_promptly_without_registered_hotkeys(self):
        hotkeys = Hotkeys(Mock())
        with patch.object(hotkeys, "read_pressed", return_value=set()):
            hotkeys.start()
            self.assertTrue(hotkeys.ready.is_set())
            hotkeys.close()
        self.assertFalse(hotkeys.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
