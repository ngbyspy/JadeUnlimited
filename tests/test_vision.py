from pathlib import Path
import unittest

import cv2
import numpy as np

from jade_unlimited.model import Product
from jade_unlimited.vision import HEIGHT, WIDTH, SLOTS, Recognizer, content_difference, content_regions, difference, normalize

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return cv2.imdecode(np.fromfile(FIXTURES / f"{name}.png", dtype=np.uint8), cv2.IMREAD_COLOR)


def place_card(image, card, slot):
    rect = SLOTS[slot]
    image[rect.y:rect.y + rect.height, rect.x:rect.x + rect.width] = card


def animated_background(frame, value):
    result = frame.copy()
    for index, slot in enumerate(SLOTS):
        result[slot.y:slot.y + slot.height, slot.x:slot.x + slot.width] = value
        for rect in content_regions(index):
            result[rect.y:rect.y + rect.height, rect.x:rect.x + rect.width] = rect.crop(frame)
    return result


class VisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.recognizer = Recognizer()

    def test_supplied_examples_ignore_unrelated_cards(self):
        for name, slot, product in (("health", 0, Product.HEALTH), ("gold", 2, Product.GOLD)):
            with self.subTest(name=name):
                scene = self.recognizer.read(fixture(name))
                self.assertTrue(scene.shop_visible)
                self.assertEqual([(offer.slot, offer.product) for offer in scene.offers], [(slot, product)])

    def test_both_products_work_in_every_slot(self):
        for name, source_slot, product in (("health", 0, Product.HEALTH), ("gold", 2, Product.GOLD)):
            base = fixture(name)
            target = SLOTS[source_slot].crop(base).copy()
            unrelated = SLOTS[1].crop(base).copy()
            for destination in range(3):
                image = base.copy()
                for slot in range(3):
                    place_card(image, unrelated, slot)
                place_card(image, target, destination)
                scene = self.recognizer.read(image)
                self.assertEqual([(offer.slot, offer.product) for offer in scene.offers], [(destination, product)])

    def test_scaled_letterboxed_capture(self):
        base = fixture("gold")
        for width, height in ((1280, 720), (1600, 900), (2560, 1440)):
            with self.subTest(size=(width, height)):
                scaled = cv2.resize(base, (width, height))
                letterboxed = cv2.copyMakeBorder(scaled, 32, 32, 0, 0, cv2.BORDER_CONSTANT)
                normalized, viewport = normalize(letterboxed)
                self.assertEqual(normalized.shape[:2], (HEIGHT, WIDTH))
                self.assertEqual(viewport.top, 32)
                self.assertEqual(viewport.screen_point((960, 540)), (width // 2, height // 2 + 32))
                scene = self.recognizer.read(normalized)
                self.assertTrue(scene.shop_visible)
                self.assertEqual([offer.product for offer in scene.offers], [Product.GOLD])

    def test_wrong_price_not_accepted_despite_correct_icon(self):
        image = fixture("health")
        # Replace the 5-gold price area with the unrelated 15-gold offer price.
        image[386:426, 684:748] = image[386:426, 936:1000].copy()
        self.assertEqual(self.recognizer.read(image).offers, ())

    def test_shop_anchor_required_and_black_screen_rejected(self):
        image = fixture("health")
        image[49:98, 925:1145] = 0
        scene = self.recognizer.read(image)
        self.assertFalse(scene.shop_visible)
        self.assertEqual(scene.offers, ())
        self.assertFalse(self.recognizer.read(np.zeros((1080, 1920, 3), dtype=np.uint8)).shop_visible)

    def test_disabled_grayscale_icon_not_bought(self):
        image = fixture("health")
        rect = SLOTS[0]
        gray = cv2.cvtColor(rect.crop(image), cv2.COLOR_BGR2GRAY)
        place_card(image, cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), 0)
        self.assertEqual(self.recognizer.read(image).offers, ())

    def test_animated_decoration_is_ignored_but_changed_items_are_detected(self):
        before = fixture("health")
        decorated = animated_background(before, 170)
        self.assertGreater(difference(before, decorated), 2.5)
        self.assertEqual(content_difference(before, decorated), 0)
        self.assertGreater(content_difference(before, fixture("gold")), 3)
        self.assertEqual([offer.product for offer in self.recognizer.read(decorated).offers], [Product.HEALTH])

    def test_blank_transition_is_not_a_ready_lineup(self):
        frame = fixture("health")
        for index in range(3):
            for rect in content_regions(index):
                frame[rect.y:rect.y + rect.height, rect.x:rect.x + rect.width] = 30
        scene = self.recognizer.read(frame)
        self.assertTrue(scene.shop_visible)
        self.assertFalse(any(scene.ready_slots))


if __name__ == "__main__":
    unittest.main()
