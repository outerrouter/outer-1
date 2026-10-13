import unittest

from app.parser import parse_post
from app.scoring import deal_score


class ParserTests(unittest.TestCase):
    def test_classic_post(self):
        p = parse_post("🔥 Boat Airdopes 141 TWS Earbuds @ ₹999 (MRP ₹4,490)\nhttps://amzn.to/abc")
        self.assertEqual(p.title, "Boat Airdopes 141 TWS Earbuds")
        self.assertEqual((p.price, p.mrp), (999.0, 4490.0))
        self.assertAlmostEqual(p.discount, 77.8, places=1)
        self.assertEqual(p.urls, ["https://amzn.to/abc"])
        self.assertFalse(p.is_free)

    def test_generic_header_line_skipped_and_coupon(self):
        p = parse_post("Price Drop Alert 🚨\nSamsung Galaxy M14 5G\nPrice: ₹9,999 MRP: ₹17,999\nCoupon: SAVE500\nhttps://www.amazon.in/dp/B0CXYZ1234")
        self.assertEqual(p.title, "Samsung Galaxy M14 5G")
        self.assertEqual((p.price, p.mrp, p.coupon), (9999.0, 17999.0, "SAVE500"))

    def test_free_with_worth_is_not_a_price(self):
        p = parse_post("Loot 😍\nFree Redmi Pad worth ₹14999 FREE\nhttps://fkrt.it/xyz")
        self.assertTrue(p.is_free)
        self.assertEqual(p.price, 0.0)
        self.assertEqual(p.mrp, 14999.0)
        self.assertTrue(p.title.startswith("Free Redmi Pad"))

    def test_free_delivery_is_not_free(self):
        p = parse_post("Cotton Kurti Set @ ₹399 + Free Delivery\nhttps://www.meesho.com/x/p/3abcde")
        self.assertFalse(p.is_free)
        self.assertEqual(p.price, 399.0)

    def test_end_notices(self):
        self.assertTrue(parse_post("Deal ended ❌ https://amzn.to/abc").ended)
        self.assertTrue(parse_post("Boat earbuds stock over").ended)
        self.assertFalse(parse_post("Order over ₹499 gets cashback @ ₹299 https://amzn.to/q").ended)

    def test_scam_flags(self):
        p = parse_post("Claim free iPhone! Send your OTP and pay ₹99 via UPI. Forward this to 10 groups. app.apk")
        for flag in ("otp_request", "payment_request", "forward_chain", "apk"):
            self.assertIn(flag, p.flags)

    def test_extreme_discount_flag(self):
        p = parse_post("Gaming Laptop @ ₹999 MRP ₹99,999\nhttps://amzn.to/a")
        self.assertIn("extreme_discount", p.flags)

    def test_unreadable_post_has_no_price(self):
        p = parse_post("Check this out https://amzn.to/abc")
        self.assertIsNone(p.price)


class ScoreTests(unittest.TestCase):
    def test_score_orders_sensibly(self):
        good = deal_score(price=999, discount=70, sources=4, trust=0.8, flags=[], verified=False)
        weak = deal_score(price=999, discount=70, sources=1, trust=0.5, flags=[], verified=False)
        scam = deal_score(price=999, discount=95, sources=1, trust=0.5, flags=["otp_request", "extreme_discount"], verified=False)
        self.assertGreater(good, weak)
        self.assertGreater(weak, scam)
        self.assertTrue(0 <= scam <= 100 and 0 <= good <= 100)


if __name__ == "__main__":
    unittest.main()
