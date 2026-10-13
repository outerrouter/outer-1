"""Demo posts so you can see the app working before Telegram is connected (Settings -> load demo data)."""

DEMO_POSTS = [
    ("Demo Deals A", "🔥 boAt Airdopes 141 TWS Earbuds @ ₹999 (MRP ₹4,490)\nhttps://www.amazon.in/dp/B0DEMO0001"),
    ("Demo Deals B", "boAt Airdopes 141 Bluetooth Earbuds Price: ₹999 MRP: ₹4490\nhttps://www.amazon.in/dp/B0DEMO0001"),
    ("Demo Deals C", "Loot 😍 boAt Airdopes 141 only ₹999\nhttps://www.amazon.in/dp/B0DEMO0001"),
    ("Demo Deals A", "Samsung Galaxy M14 5G (6GB/128GB)\nPrice: ₹9,999 MRP: ₹17,999\nCoupon: SAVE500\nhttps://www.flipkart.com/samsung-m14/p/itmdemo0002?pid=MOBDEMO00002"),
    ("Demo Deals B", "Cotton Kurti Set for Women @ ₹399 + Free Delivery\nhttps://www.meesho.com/cotton-kurti-set/p/3demo03"),
    ("Demo Deals A", "Free Redmi Pad worth ₹14999 FREE\nhttps://www.flipkart.com/redmi-pad/p/itmdemo0004?pid=TABDEMO00004"),
    ("Demo Deals B", "Free Redmi Pad worth ₹14999 FREE\nhttps://www.flipkart.com/redmi-pad/p/itmdemo0004?pid=TABDEMO00004"),
    ("Demo Deals C", "Free Redmi Pad worth ₹14999 FREE\nhttps://www.flipkart.com/redmi-pad/p/itmdemo0004?pid=TABDEMO00004"),
    ("Demo Deals C", "Lenovo IdeaPad Slim 3 Laptop @ ₹32,990 (MRP ₹58,390)\nhttps://www.amazon.in/dp/B0DEMO0005"),
    ("Shady Group", "iPhone 15 Pro FREE! Send OTP to claim. Forward to 10 groups\nhttps://www.amazon.in/dp/B0DEMO0006 @ ₹0"),
    ("Shady Group", "Samsung TV @ ₹4,999 only!!\nhttps://amazon-offers.xyz/win-now"),
]


async def load_demo(pipeline) -> int:
    import time
    base = int(time.time() * 1000)
    for i, (channel, text) in enumerate(DEMO_POSTS):
        await pipeline.ingest({"channel": channel, "channel_id": channel, "msg_id": base + i, "text": text,
                               "no_probe": True})
    return len(DEMO_POSTS)
