"""Rule-based post parser. Handles the common deal-post shapes WITHOUT any AI request.

Anything it cannot read confidently is handed to the batched AI parser (see pipeline.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .links import extract_urls

NUM = r"(\d[\d,]*(?:\.\d+)?)"
CUR = r"(?:₹|\b(?:rs|inr)\b\.?)"
PRICE_RES = [
    re.compile(CUR + r"\s*" + NUM, re.I),
    re.compile(r"@\s*" + CUR + r"?\s*" + NUM, re.I),
    re.compile(r"\bprice\s*[:=\-]\s*" + CUR + r"?\s*" + NUM, re.I),
]
MRP_RES = [
    re.compile(r"(?:\bmrp\b|\bm\.r\.p\b\.?|\bwas\b|\boriginal(?:\s+price)?\b|\bactual(?:\s+price)?\b)\s*[:\-]?\s*" + CUR + r"?\s*" + NUM, re.I),
    re.compile(r"~+\s*" + CUR + r"?\s*" + NUM + r"\s*~+", re.I),
    re.compile(r"\b(?:worth|value|valued\s+at)\s*[:\-]?\s*" + CUR + r"\s*" + NUM, re.I),
]
DISC_RES = [
    re.compile(r"(\d{1,3})\s*%\s*(?:off|discount)", re.I),
    re.compile(r"(?:flat|upto|up\s+to)\s*(\d{1,3})\s*%", re.I),
]
COUPON_RE = re.compile(r"(?i:coupon|code|promo)(?:\s*code)?\s*[:\-=]?\s*([A-Z][A-Z0-9]{3,19})\b")
FREE_RE = re.compile(r"\b(?:free|freebie)\b(?!\s*(?:delivery|shipping|cancellation|returns?|replacement|trial|demo|installation|size|gift))", re.I)
END_RES = [
    re.compile(r"\b(?:deal|offer|loot|stock|sale|coupon|price)\s+(?:is\s+)?(?:over|ended|expired|finished|closed)\b", re.I),
    re.compile(r"\b(?:ended|expired|sold\s+out|out\s+of\s+stock|oos|stock\s+(?:over|finish(?:ed)?)|not\s+working|khatam|khatm)\b", re.I),
    re.compile(r"\bprice\s+(?:increased|hiked|changed)\b", re.I),
    re.compile(r"खत्म"),
]
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200d]")
GENERIC_RE = re.compile(r"^(?:loot|deal|offer|lightning|steal|price\s*drop|hot|big|alert|best|live\s*now|fast|sale)\b[\s!:|\-–]*(?:deal|offer|alert|loot)?[\s!:|\-–]*$", re.I)
LEAD_RE = re.compile(r"^(?:loot|deal|offer|lightning|steal|price\s*drop|alert|hot\s*deal|big\s*deal)[\s!:|\-–]+", re.I)
CUT_RE = re.compile(r"\s*(?:@|₹|\b(?:rs|inr)\b\.?\s*\d|\bmrp\b|\bprice\s*[:=\-]|\bat\s+₹|\bfor\s+₹|\bjust\s+₹|\bworth\b)", re.I)

SCAM_RES = {
    "otp_request": re.compile(r"\botp\b", re.I),
    "payment_request": re.compile(r"\b(?:pay|send|transfer)\b[^.\n]{0,40}\b(?:upi|gpay|phonepe|paytm|advance|registration fee)\b|\badvance\s+payment\b", re.I),
    "apk": re.compile(r"\.apk\b", re.I),
    "forward_chain": re.compile(r"\b(?:forward|share)\b[^.\n]{0,30}\b\d+\s*(?:groups?|friends?|contacts?|people)\b", re.I),
    "lottery": re.compile(r"\b(?:lottery|jackpot|lucky\s+draw|you\s+(?:have\s+)?won)\b", re.I),
    "dm_bait": re.compile(r"\b(?:dm\s+me|whatsapp\s+me|contact\s+me\s+for)\b", re.I),
}


@dataclass
class Parsed:
    title: str = ""
    price: float | None = None
    mrp: float | None = None
    discount: float | None = None
    coupon: str | None = None
    is_free: bool = False
    ended: bool = False
    urls: list = field(default_factory=list)
    flags: list = field(default_factory=list)


def _num(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _title(text: str) -> str:
    for line in text.splitlines():
        s = re.sub(r"https?://\S+", "", line)
        s = re.sub(r"[*_`~|>#\[\]]+", "", s)
        s = EMOJI_RE.sub("", s)
        s = re.sub(r"\s+", " ", s).strip(" -–—:•·.,!")
        if not s or GENERIC_RE.match(s):
            continue
        s = LEAD_RE.sub("", s)
        s = CUT_RE.split(s, 1)[0].strip(" -–—:•·.,!(")
        if len(s) >= 6 and re.search(r"[A-Za-z\u0980-\u09FF]", s):
            return s[:140]
    return ""


def parse_post(text: str, extra_urls=()) -> Parsed:
    text = text or ""
    p = Parsed(urls=extract_urls(text, extra_urls))

    mrp_spans, mrps = [], []
    for rx in MRP_RES:
        for m in rx.finditer(text):
            value = _num(m.group(1))
            if value:
                mrps.append(value)
                mrp_spans.append((m.start(), m.end()))
    prices: list[float] = []
    for rx in PRICE_RES:
        for m in rx.finditer(text):
            if any(a <= m.start() < b for a, b in mrp_spans):
                continue
            value = _num(m.group(1))
            if value is not None and value not in prices:
                prices.append(value)

    if mrps:
        p.mrp = max(mrps)
    if prices:
        below = [v for v in prices if p.mrp is None or v < p.mrp]
        p.price = min(below) if below else prices[0]
        if p.mrp is None and len(prices) >= 2 and max(prices) >= 1.2 * min(prices):
            p.mrp = max(prices)

    for rx in DISC_RES:
        for m in rx.finditer(text):
            value = _num(m.group(1))
            if value and 0 < value <= 100:
                p.discount = max(p.discount or 0, value)
    if p.price and p.mrp and p.mrp > p.price:
        p.discount = round((p.mrp - p.price) / p.mrp * 100, 1)

    coupon = COUPON_RE.search(text)
    p.coupon = coupon.group(1) if coupon else None
    p.is_free = (p.price == 0) or (p.discount == 100) or (p.price is None and bool(FREE_RE.search(text)))
    if p.is_free:
        p.price = 0.0
    p.ended = any(rx.search(text) for rx in END_RES)
    p.title = _title(text)

    for code, rx in SCAM_RES.items():
        if rx.search(text):
            p.flags.append(code)
    if p.discount and p.discount >= 90 and not p.is_free:
        p.flags.append("extreme_discount")
    return p
