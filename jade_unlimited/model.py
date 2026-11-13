from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class Product(str, Enum):
    HEALTH = "5 金币 → 10 生命值"
    GOLD = "10 生命值 → 10 金币"


@dataclass(frozen=True)
class Offer:
    slot: int
    product: Product
    confidence: float = 1.0


@dataclass
class Account:
    health: int
    gold: int
    health_bought: int = 0
    gold_bought: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.health <= 100:
            raise ValueError("初始生命值必须在 1～100 之间")
        if self.gold < 0:
            raise ValueError("初始金币不能小于 0")

    def can_buy(self, product: Product) -> bool:
        if product == Product.HEALTH:
            return self.health < 100 and self.gold >= 5
        return self.health > 10

    def commit(self, product: Product) -> None:
        """Only call after the screen confirms the clicked offer disappeared."""
        if not self.can_buy(product):
            raise ValueError(f"当前账本不允许购买 {product.value}")
        if product == Product.HEALTH:
            self.health = min(100, self.health + 10)
            self.gold -= 5
            self.health_bought += 1
        else:
            self.health -= 10
            self.gold += 10
            self.gold_bought += 1

    @property
    def bankrupt(self) -> bool:
        return self.health <= 10 and self.gold < 5


def choose_offer(account: Account, offers: Iterable[Offer]) -> Offer | None:
    # Sell first: this makes room at the HP cap and funds a heal if gold is low.
    available = [offer for offer in offers if account.can_buy(offer.product)]
    return min(
        available,
        key=lambda offer: (offer.product != Product.GOLD, offer.slot),
        default=None,
    )
