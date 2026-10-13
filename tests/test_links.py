import asyncio
import unittest

from app import links

SHORT = {"amzn.to", "fkrt.it", "bit.ly"}


class FakeResp:
    def __init__(self, status=302, location=None):
        self.status_code = status
        self.headers = {"location": location} if location else {}


class FakeStream:
    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        return self.resp

    async def __aexit__(self, *a):
        return False


class FakeClient:
    """Maps url -> (status, location). Records every URL it was asked to connect to."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def stream(self, method, url, **kw):
        self.calls.append(url)
        status, loc = self.table[url]
        return FakeStream(FakeResp(status, loc))


class LinkTests(unittest.TestCase):
    def test_platform_matching_blocks_tricks(self):
        self.assertEqual(links.platform_of("www.amazon.in"), "amazon")
        self.assertEqual(links.platform_of("dl.flipkart.com"), "flipkart")
        self.assertIsNone(links.platform_of("amazon.in.evil.com"))
        self.assertIsNone(links.platform_of("evilamazon.in"))
        self.assertTrue(links.looks_like_brand("amazon-offers.xyz"))
        self.assertTrue(links.looks_like_brand("xn--mazon-3ve.in"))
        self.assertFalse(links.looks_like_brand("example.com"))

    def test_extract_urls_strips_markdown_and_punctuation(self):
        urls = links.extract_urls("Buy [here](https://amzn.to/abc). Also https://bit.ly/x!", ["https://fkrt.it/q"])
        self.assertEqual(urls, ["https://amzn.to/abc", "https://bit.ly/x", "https://fkrt.it/q"])

    def test_normalize_amazon_flipkart_meesho(self):
        p, pid, canon = links.normalize("https://www.amazon.in/Some-Title/dp/b0cxyz1234/ref=x?tag=foo-21&th=1")
        self.assertEqual((p, pid, canon), ("amazon", "B0CXYZ1234", "https://www.amazon.in/dp/B0CXYZ1234"))
        p, pid, canon = links.normalize("https://www.flipkart.com/boat-earbuds/p/itm123abc?pid=ACCGZ4HDXYZ&affid=zz")
        self.assertEqual((p, pid), ("flipkart", "ACCGZ4HDXYZ"))
        self.assertNotIn("affid", canon)
        p, pid, canon = links.normalize("https://www.meesho.com/cotton-kurti/p/3abcde?utm=1")
        self.assertEqual((p, pid, canon), ("meesho", "3abcde", "https://www.meesho.com/s/p/3abcde"))

    def test_resolve_follows_only_shorteners_and_never_connects_to_target(self):
        client = FakeClient({"https://amzn.to/abc": (301, "https://www.amazon.in/dp/B0CXYZ1234?tag=x")})
        info = asyncio.run(links.check_link("https://amzn.to/abc", client, SHORT))
        self.assertEqual(info.state, "ok")
        self.assertEqual(info.key, "amazon:B0CXYZ1234")
        self.assertEqual(client.calls, ["https://amzn.to/abc"])      # the shop page itself was not fetched

    def test_shortener_pointing_to_evil_domain_is_blocked_and_not_fetched(self):
        client = FakeClient({"https://bit.ly/x": (301, "https://amazon-offers.xyz/win")})
        info = asyncio.run(links.check_link("https://bit.ly/x", client, SHORT))
        self.assertEqual(info.state, "blocked")
        self.assertEqual(info.reason, "lookalike")
        self.assertEqual(client.calls, ["https://bit.ly/x"])

    def test_unknown_domain_not_fetched(self):
        client = FakeClient({})
        info = asyncio.run(links.check_link("https://random-site.com/deal", client, SHORT))
        self.assertEqual((info.state, info.reason), ("blocked", "unknown_domain"))
        self.assertEqual(client.calls, [])

    def test_userinfo_trick_blocked(self):
        info = asyncio.run(links.check_link("https://amazon.in@evil.com/x", FakeClient({}), SHORT))
        self.assertEqual(info.state, "blocked")

    def test_probe_detects_dead_and_stays_on_allowed_hosts(self):
        client = FakeClient({"https://www.amazon.in/dp/B0CXYZ1234": (404, None)})
        self.assertEqual(asyncio.run(links.probe("https://www.amazon.in/dp/B0CXYZ1234", client)), "dead")
        client = FakeClient({"https://www.amazon.in/dp/B0CXYZ1234": (302, "https://evil.com/")})
        self.assertEqual(asyncio.run(links.probe("https://www.amazon.in/dp/B0CXYZ1234", client)), "unknown")
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
