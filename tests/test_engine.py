import itertools
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from jade_unlimited.engine import Settings, Stopped, Uncertain, Worker
from jade_unlimited.model import Account, Offer, Product
from jade_unlimited.vision import SLOTS, content_regions
from jade_unlimited.windows import Bounds, InputNotSent
from test_vision import animated_background, fixture, place_card


class FakeDesktop:
    def __init__(self, frame, after=None, click_error=False):
        self.frame = frame
        self.after = after
        self.clicks = []
        self.click_error = click_error

    def capture(self):
        return self.frame.copy(), Bounds(0, 0, 1920, 1080)

    def click(self, point, bounds):
        self.clicks.append(point)
        if self.click_error:
            raise RuntimeError("injected input failure")
        if self.after is not None:
            self.frame = self.after.copy()


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.worker = Worker(Settings(123, 50, 25), lambda kind, value: self.events.append((kind, value)))
        self.worker.active.set()
        self.worker.wait = lambda _seconds: None
        self.foreground = patch("jade_unlimited.engine.is_foreground", return_value=True)
        self.foreground.start()
        self.addCleanup(self.foreground.stop)

    def test_confirmed_purchase_commits_once(self):
        before = fixture("health")
        after = before.copy()
        place_card(after, SLOTS[1].crop(before), 0)
        desktop = FakeDesktop(before, after)
        self.assertTrue(self.worker.perform(desktop, before, Offer(0, Product.HEALTH)))
        self.assertEqual(len(desktop.clicks), 1)
        self.assertEqual((self.worker.account.health, self.worker.account.gold), (60, 20))
        self.assertFalse(self.worker.pending)

    def test_unconfirmed_purchase_never_retries_or_changes_ledger(self):
        before = fixture("health")
        desktop = FakeDesktop(before)
        with patch("jade_unlimited.engine.time.monotonic", side_effect=itertools.count()):
            with self.assertRaises(Uncertain):
                self.worker.perform(desktop, before, Offer(0, Product.HEALTH))
        self.assertEqual(len(desktop.clicks), 1)
        self.assertEqual((self.worker.account.health, self.worker.account.gold), (50, 25))
        self.assertTrue(self.worker.pending)

    def test_input_failure_is_uncertain_and_never_commits(self):
        before = fixture("health")
        desktop = FakeDesktop(before, click_error=True)
        with self.assertRaises(Uncertain):
            self.worker.perform(desktop, before, Offer(0, Product.HEALTH))
        self.assertEqual((self.worker.account.health, self.worker.account.gold), (50, 25))
        self.assertTrue(self.worker.pending)

    def test_known_rejected_input_does_not_report_possible_purchase(self):
        before = fixture("health")
        desktop = FakeDesktop(before)
        desktop.click = Mock(side_effect=InputNotSent("SetCursorPos failed before button input"))
        with self.assertRaises(InputNotSent):
            self.worker.perform(desktop, before, Offer(0, Product.HEALTH))
        self.assertFalse(self.worker.pending)
        self.assertEqual((self.worker.account.health, self.worker.account.gold), (50, 25))
        desktop.click.assert_called_once()

    def test_free_refresh_updates_only_after_changed_cards(self):
        self.worker.account = Account(100, 25)
        before = fixture("health")
        desktop = FakeDesktop(before, fixture("gold"))
        self.assertTrue(self.worker.perform(desktop, before, None))
        self.assertEqual(self.worker.refreshes, 1)
        self.assertEqual((self.worker.account.health, self.worker.account.gold), (100, 25))

    def test_pause_stop_and_stale_card_prevent_clicks(self):
        before = fixture("health")
        desktop = FakeDesktop(before)
        self.worker.active.clear()
        self.assertFalse(self.worker.perform(desktop, before, None))
        self.worker.active.set()
        self.worker.request_stop()
        with self.assertRaises(Stopped):
            self.worker.perform(desktop, before, None)
        self.assertEqual(desktop.clicks, [])
        self.worker.stop_event.clear()
        desktop.frame = fixture("gold")
        self.assertFalse(self.worker.perform(desktop, before, Offer(0, Product.HEALTH)))
        self.assertEqual(desktop.clicks, [])

    def test_lost_foreground_cancels_before_input(self):
        before = fixture("health")
        desktop = FakeDesktop(before)
        with patch("jade_unlimited.engine.is_foreground", return_value=False):
            with self.assertRaises(RuntimeError):
                self.worker.perform(desktop, before, None)
        self.assertEqual(desktop.clicks, [])

    def test_start_outside_game_waits_then_runs_on_foreground(self):
        foreground = False
        waits = 0
        desktop = Mock()
        self.worker.desktop_factory = Mock(return_value=desktop)
        frame = fixture("health")
        scene = self.worker.recognizer.read(frame)
        self.worker.stable_scene = Mock(return_value=(frame, scene, None, None))
        self.worker.perform = Mock(side_effect=lambda *_args: self.worker.request_stop())

        def wait_for_focus(_seconds):
            nonlocal waits, foreground
            waits += 1
            self.assertTrue(self.worker.active.is_set())
            self.assertTrue(self.worker.waiting_for_foreground.is_set())
            self.worker.perform.assert_not_called()
            if waits == 2:
                foreground = True

        self.worker.wait = wait_for_focus
        self.worker.resume()
        with TemporaryDirectory() as directory, patch("jade_unlimited.engine.data_directory", return_value=Path(directory)), patch("jade_unlimited.engine.is_foreground", side_effect=lambda _hwnd: foreground), patch("jade_unlimited.engine.cursor_at_corner", return_value=False):
            self.worker.run()
        self.assertEqual(waits, 2)
        self.worker.perform.assert_called_once()
        self.assertFalse(self.worker.waiting_for_foreground.is_set())
        desktop.close.assert_called_once()

    def test_focus_loss_after_running_still_pauses_and_requires_explicit_resume(self):
        foreground = True
        frame = fixture("health")
        self.worker.desktop_factory = Mock(return_value=Mock())
        self.worker.stable_scene = Mock(return_value=(frame, self.worker.recognizer.read(frame), None, None))

        def lose_focus(*_args):
            nonlocal foreground
            foreground = False

        original_pause = self.worker.pause

        def observe_pause(reason):
            nonlocal foreground
            original_pause(reason)
            foreground = True
            self.assertFalse(self.worker.active.is_set())
            self.assertFalse(self.worker.waiting_for_foreground.is_set())
            self.worker.request_stop()

        self.worker.perform = Mock(side_effect=lose_focus)
        self.worker.pause = Mock(side_effect=observe_pause)
        self.worker.resume()
        with TemporaryDirectory() as directory, patch("jade_unlimited.engine.data_directory", return_value=Path(directory)), patch("jade_unlimited.engine.is_foreground", side_effect=lambda _hwnd: foreground), patch("jade_unlimited.engine.cursor_at_corner", return_value=False):
            self.worker.run()
        self.worker.perform.assert_called_once()
        self.worker.pause.assert_called_once()

    def test_manual_pause_cancels_waiting_for_foreground(self):
        self.worker.resume()
        self.assertTrue(self.worker.waiting_for_foreground.is_set())
        self.worker.pause()
        self.assertFalse(self.worker.waiting_for_foreground.is_set())
        self.assertFalse(self.worker.active.is_set())

    def test_refresh_uses_its_own_short_delay_and_two_changed_observations(self):
        self.worker.account = Account(100, 25)
        waits = []
        self.worker.wait = waits.append
        before = fixture("health")
        desktop = FakeDesktop(before, fixture("gold"))
        self.worker.perform(desktop, before, None)
        self.assertEqual(waits, [0.08, 0.04])
        self.assertEqual(self.worker.verification_frames, 2)
        self.assertIsNotNone(self.worker.ready_scene)

    def test_animation_only_cannot_confirm_refresh(self):
        self.worker.account = Account(100, 25)
        before = fixture("health")
        desktop = FakeDesktop(before, animated_background(before, 170))
        with patch("jade_unlimited.engine.time.monotonic", side_effect=itertools.count()):
            with self.assertRaises(Uncertain):
                self.worker.perform(desktop, before, None)
        self.assertEqual(self.worker.refreshes, 0)

    def test_blank_transition_then_background_animation_confirms_only_real_lineup(self):
        self.worker.account = Account(100, 25)
        before, after = fixture("health"), fixture("gold")
        blank = after.copy()
        for index in range(3):
            for rect in content_regions(index):
                blank[rect.y:rect.y + rect.height, rect.x:rect.x + rect.width] = 30
        desktop = FakeDesktop(before)
        frames = iter([before, blank, blank, animated_background(after, 80), animated_background(after, 170)])
        desktop.capture = Mock(side_effect=lambda: (next(frames).copy(), Bounds(0, 0, 1920, 1080)))
        self.worker.perform(desktop, before, None)
        self.assertEqual(self.worker.verification_frames, 4)
        self.assertEqual([offer.product for offer in self.worker.ready_scene[1].offers], [Product.GOLD])

    def test_last_moment_legal_target_prevents_refresh(self):
        before = fixture("health")
        desktop = FakeDesktop(before)
        self.assertFalse(self.worker.perform(desktop, before, None))
        self.assertEqual(desktop.clicks, [])

    def test_confirmed_lineup_is_reused_and_new_target_is_bought_before_refresh(self):
        first, gold = fixture("health"), fixture("gold")
        purchased = gold.copy()
        place_card(purchased, SLOTS[0].crop(gold), 2)
        desktop = FakeDesktop(first)
        desktop.close = Mock()

        def advance(point, _bounds):
            desktop.clicks.append(point)
            desktop.frame = (gold if len(desktop.clicks) == 1 else purchased).copy()

        desktop.click = advance
        self.worker.settings = Settings(123, 100, 25, target_gold=35)
        self.worker.account = Account(100, 25)
        self.worker.desktop_factory = lambda _hwnd: desktop
        self.worker.stable_scene = Mock(wraps=self.worker.stable_scene)
        with TemporaryDirectory() as directory, patch("jade_unlimited.engine.data_directory", return_value=Path(directory)), patch("jade_unlimited.engine.cursor_at_corner", return_value=False):
            self.worker.run()
        self.worker.stable_scene.assert_called_once()
        self.assertEqual(len(desktop.clicks), 2)
        self.assertEqual(self.worker.refreshes, 1)
        self.assertEqual((self.worker.account.health, self.worker.account.gold), (90, 35))


if __name__ == "__main__":
    unittest.main()
