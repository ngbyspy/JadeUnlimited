import unittest

from jade_unlimited.model import Account, Offer, Product, choose_offer


class AccountTests(unittest.TestCase):
    def test_health_bounds_and_gold_cost(self):
        self.assertFalse(Account(100, 25).can_buy(Product.HEALTH))
        self.assertFalse(Account(90, 4).can_buy(Product.HEALTH))
        account = Account(95, 5)
        account.commit(Product.HEALTH)
        self.assertEqual((account.health, account.gold), (100, 0))

    def test_sell_requires_more_than_ten_health(self):
        for health in range(1, 11):
            with self.subTest(health=health):
                account = Account(health, 25)
                self.assertFalse(account.can_buy(Product.GOLD))
                with self.assertRaises(ValueError):
                    account.commit(Product.GOLD)
                self.assertEqual((account.health, account.gold), (health, 25))
        account = Account(11, 0)
        account.commit(Product.GOLD)
        self.assertEqual((account.health, account.gold), (1, 10))

    def test_sell_first_makes_room_at_cap_and_a_cycle_profits_five(self):
        offers = [Offer(0, Product.HEALTH), Offer(2, Product.GOLD)]
        for health in range(11, 101):
            with self.subTest(health=health):
                account = Account(health, 0)
                chosen = choose_offer(account, offers)
                self.assertEqual(chosen.product, Product.GOLD)
                account.commit(chosen.product)
                account.commit(Product.HEALTH)
                self.assertEqual((account.health, account.gold), (health, 5))

    def test_no_affordable_trade_and_invalid_inputs(self):
        self.assertTrue(Account(10, 4).bankrupt)
        self.assertFalse(Account(10, 5).bankrupt)
        self.assertIsNone(choose_offer(Account(10, 4), [Offer(0, Product.GOLD), Offer(1, Product.HEALTH)]))
        for health, gold in ((0, 0), (101, 0), (1, -1)):
            with self.assertRaises(ValueError):
                Account(health, gold)


if __name__ == "__main__":
    unittest.main()
