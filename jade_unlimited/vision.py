from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .model import Offer, Product

WIDTH, HEIGHT = 1920, 1080
ASSETS = Path(__file__).resolve().parent / "assets"


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def crop(self, image: np.ndarray) -> np.ndarray:
        return image[self.y:self.y + self.height, self.x:self.x + self.width]

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2


SLOTS = (Rect(537, 175, 222, 263), Rect(789, 175, 222, 263), Rect(1041, 175, 222, 263))
REFRESH = Rect(803, 494, 197, 39)
SHOP_AREA = Rect(521, 170, 760, 272)


def content_regions(slot: int) -> tuple[Rect, Rect]:
    card = SLOTS[slot]
    return Rect(card.x + 66, card.y + 62, 92, 92), Rect(card.x + 8, card.y + 211, card.width - 16, 43)


@dataclass(frozen=True)
class Viewport:
    left: int
    top: int
    width: int
    height: int

    def screen_point(self, point: tuple[int, int]) -> tuple[int, int]:
        return (
            self.left + round(point[0] * self.width / WIDTH),
            self.top + round(point[1] * self.height / HEIGHT),
        )


def normalize(frame: np.ndarray) -> tuple[np.ndarray, Viewport]:
    """Fit a centered 16:9 viewport (including ordinary letterboxing)."""
    height, width = frame.shape[:2]
    if width < 800 or height < 450:
        raise ValueError("游戏客户区太小，请调整到至少 800×450")
    if width / height > WIDTH / HEIGHT:
        view_height = height
        view_width = round(height * WIDTH / HEIGHT)
    else:
        view_width = width
        view_height = round(width * HEIGHT / WIDTH)
    left, top = (width - view_width) // 2, (height - view_height) // 2
    viewport = Viewport(left, top, view_width, view_height)
    cropped = frame[top:top + view_height, left:left + view_width]
    return (cropped if cropped.shape[:2] == (HEIGHT, WIDTH) else cv2.resize(cropped, (WIDTH, HEIGHT), interpolation=cv2.INTER_LINEAR)), viewport


@dataclass(frozen=True)
class Scene:
    shop_visible: bool
    offers: tuple[Offer, ...]
    title_score: float
    refresh_score: float
    scores: tuple[dict[str, float], ...]
    ready_slots: tuple[bool, ...] = (True, True, True)


def difference(before: np.ndarray, after: np.ndarray, rect: Rect = SHOP_AREA) -> float:
    a = cv2.cvtColor(rect.crop(before), cv2.COLOR_BGR2GRAY)
    b = cv2.cvtColor(rect.crop(after), cv2.COLOR_BGR2GRAY)
    return float(cv2.absdiff(a, b).mean())


def content_difference(before: np.ndarray, after: np.ndarray, slot: int | None = None) -> float:
    """Ignore purple card/background effects; require every card's content to settle."""
    scores = []
    for index in range(3) if slot is None else (slot,):
        regions = content_regions(index)
        scores.append(sum(difference(before, after, rect) * rect.width * rect.height for rect in regions) / sum(rect.width * rect.height for rect in regions))
    return max(scores)


class Recognizer:
    def __init__(self, threshold: float = 0.88):
        if not 0.80 <= threshold <= 0.99:
            raise ValueError("识别阈值应在 0.80～0.99 之间")
        self.threshold = threshold
        self.templates: dict[str, np.ndarray] = {}
        for name in ("title", "refresh", "health_icon", "health_footer", "gold_icon", "gold_footer"):
            template = cv2.imdecode(np.fromfile(ASSETS / f"{name}.png", dtype=np.uint8), cv2.IMREAD_COLOR)
            if template is None:
                raise FileNotFoundError(f"模板读取失败：{name}")
            self.templates[name] = template
        self.gray_templates = {name: cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) for name, img in self.templates.items()}

    def match(self, image: np.ndarray, name: str, *, color: bool = False) -> float:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        template = self.gray_templates[name]
        best = 0.0
        # Small rendering/UI-scale differences between the two supplied captures.
        for scale in (0.97, 1.0, 1.03):
            resized = cv2.resize(template, None, fx=scale, fy=scale)
            th, tw = resized.shape
            if th > gray.shape[0] or tw > gray.shape[1]:
                continue
            if float(resized.std()) < 1.0:
                continue
            result = cv2.matchTemplate(gray, resized, cv2.TM_CCOEFF_NORMED)
            _, score, _, location = cv2.minMaxLoc(result)
            if color and score > best:
                candidate = image[location[1]:location[1] + th, location[0]:location[0] + tw]
                original = cv2.resize(self.templates[name], (tw, th))
                # Correlation alone also matches grayscale/disabled icons.
                error = float(cv2.absdiff(candidate, original).mean())
                saturation = float(cv2.cvtColor(candidate, cv2.COLOR_BGR2HSV)[:, :, 1].mean())
                expected_sat = float(cv2.cvtColor(original, cv2.COLOR_BGR2HSV)[:, :, 1].mean())
                if error > 45 or saturation < expected_sat * 0.55:
                    score = 0.0
            best = max(best, float(score))
        return best

    def read(self, frame: np.ndarray) -> Scene:
        if frame.shape[:2] != (HEIGHT, WIDTH):
            raise ValueError("Recognizer.read 需要标准化后的 1920×1080 画面")
        title = self.match(Rect(925, 49, 220, 49).crop(frame), "title")
        refresh = self.match(Rect(778, 482, 249, 63).crop(frame), "refresh")
        visible = min(title, refresh) >= self.threshold
        offers: list[Offer] = []
        diagnostics: list[dict[str, float]] = []
        ready_slots = []
        for index, slot in enumerate(SLOTS):
            icon, footer = (cv2.cvtColor(rect.crop(frame), cv2.COLOR_BGR2GRAY) for rect in content_regions(index))
            # A blank transition must not be mistaken for a finished, empty shop.
            ready_slots.append(float(icon.std()) > 14 and float(footer.std()) > 15)
            icon_area = Rect(slot.x + 18, slot.y + 26, slot.width - 36, 169).crop(frame)
            footer_area = Rect(slot.x + 5, slot.y + 207, slot.width - 10, 51).crop(frame)
            scores = {}
            for prefix, product in (("health", Product.HEALTH), ("gold", Product.GOLD)):
                icon = self.match(icon_area, f"{prefix}_icon", color=True)
                footer = self.match(footer_area, f"{prefix}_footer", color=True)
                scores[prefix] = min(icon, footer)
            diagnostics.append(scores)
            matches = [name for name, score in scores.items() if score >= self.threshold]
            # Reject ambiguous cards. Both the icon and exact price/name must match.
            if visible and len(matches) == 1:
                name = matches[0]
                offers.append(Offer(index, Product.HEALTH if name == "health" else Product.GOLD, scores[name]))
        return Scene(visible, tuple(offers), title, refresh, tuple(diagnostics), tuple(ready_slots))


def annotate(frame: np.ndarray, scene: Scene) -> np.ndarray:
    result = frame.copy()
    by_slot = {offer.slot: offer for offer in scene.offers}
    for index, rect in enumerate(SLOTS):
        offer = by_slot.get(index)
        color = (40, 220, 60) if offer else (140, 140, 140)
        cv2.rectangle(result, (rect.x, rect.y), (rect.x + rect.width, rect.y + rect.height), color, 3)
        label = f"{index + 1}: {('HP +10' if offer.product == Product.HEALTH else 'GOLD +10')} {offer.confidence:.3f}" if offer else f"{index + 1}: other"
        cv2.putText(result, label, (rect.x, rect.y - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.58, color, 2)
    cv2.rectangle(result, REFRESH.center, REFRESH.center, (0, 0, 255), 10)
    cv2.putText(result, f"Shop: {scene.shop_visible} | title {scene.title_score:.3f} refresh {scene.refresh_score:.3f}", (520, 126), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
    return result
