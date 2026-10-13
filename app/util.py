"""Small shared helpers."""
from __future__ import annotations

import json
import re

STOPWORDS = {
    "a", "an", "the", "and", "or", "for", "to", "of", "in", "on", "at", "me", "my", "i", "want", "need",
    "under", "below", "less", "than", "price", "cheap", "best", "buy", "get", "alert", "track", "watch",
    "notify", "tell", "please", "rs", "inr", "with", "free", "deal", "offer",
    "আমার", "আমি", "চাই", "দাও", "দিন", "এই", "একটা", "একটি", "কম", "দামে", "দাম", "নিচে", "টাকা",
    "জানাও", "কমলে", "খুঁজে", "খুঁজো", "কিনতে", "লাগবে", "প্লিজ", "পণ্য", "অফার",
    "নিচ", "কমে", "পেলে", "পেলেই", "জানাবে", "সেরা", "আজ", "আজকের", "কোন", "কোনগুলো", "কী", "কি", "ডিল", "deals",
    "দেখাও", "দেখান", "বলো", "বলুন", "হলে", "করে", "করো", "দরকার", "চাইছি",
}
_BN_SUFFIX = re.compile(r"(?:গুলো|গুলি|দের|েরই|ের|র|কে|ে)$")


def safe_json(text: str):
    """Parse JSON from a model reply; tolerate code fences and stray text. Raises ValueError."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except ValueError:
        i, j = t.find("{"), t.rfind("}")
        if 0 <= i < j:
            return json.loads(t[i:j + 1])
        raise


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[\w\u0980-\u09FF]+", (text or "").lower())
    out = []
    for w in words:
        base = _BN_SUFFIX.sub("", w) if re.search(r"[\u0980-\u09FF]", w) else w
        if w in STOPWORDS or base in STOPWORDS or len(w) <= 1 or w.isdigit():
            continue
        out.append(w)
    return out


def to_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(str(value).replace(",", "").replace("₹", "").strip())
    except ValueError:
        return None
    return num if num >= 0 else None


def fmt_price(value) -> str:
    if value is None:
        return "—"
    return "₹{:,.0f}".format(value)


def deal_out(row: dict | None) -> dict | None:
    """Shape a deals row for the API / UI."""
    if not row:
        return None
    out = dict(row)
    try:
        out["flags"] = json.loads(out.get("flags") or "[]")
    except ValueError:
        out["flags"] = []
    try:
        out["verdict_data"] = json.loads(out["verdict_json"]) if out.get("verdict_json") else None
    except ValueError:
        out["verdict_data"] = None
    out.pop("verdict_json", None)
    image = out.get("image")
    if image and not image.startswith(("http://", "https://", "/")):
        out["image"] = "/media/" + image
    return out
