"""Deal score (0-100) and a rule-based fallback verdict used when no AI is available."""
from __future__ import annotations

SEVERE = {"otp_request", "payment_request", "apk", "forward_chain", "lottery", "lookalike"}


def deal_score(*, price, discount, sources: int, trust: float, flags, verified: bool, is_low: bool = False) -> float:
    score = min(discount or 0, 80) * 0.5            # up to 40: how big the claimed discount is
    score += min(max(sources, 1), 5) * 6            # up to 30: how many channels posted it
    score += max(0.0, min(trust, 1.0)) * 20         # up to 20: how reliable those channels have been
    if verified:
        score += 10
    if is_low:
        score += 5
    flags = set(flags or [])
    score -= 35 * len(flags & SEVERE)
    if "extreme_discount" in flags:
        score -= 20
    if "dm_bait" in flags:
        score -= 10
    if price is None:
        score -= 15
    return round(max(0.0, min(100.0, score)), 1)


def rule_verdict(deal: dict, flags) -> dict:
    """Same shape as the AI verdict. Honest about being rules-only (low confidence)."""
    flags = set(flags or [])
    severe = sorted(flags & SEVERE)
    score = deal.get("score") or 0
    if severe:
        return {"verdict": "avoid", "confidence": 80, "scam_likelihood": "high", "matches_request": None,
                "summary": "সন্দেহজনক লক্ষণ পাওয়া গেছে, তাই এড়িয়ে চলাই ভালো।",
                "reasons": ["rule: " + f for f in severe], "risks": ["scam হওয়ার সম্ভাবনা বেশি"],
                "fit": "", "source": "rules"}
    if score >= 75 and (deal.get("sources") or 1) >= 2:
        return {"verdict": "buy", "confidence": 55, "scam_likelihood": "low", "matches_request": None,
                "summary": "একাধিক channel-এ এসেছে এবং কোনো scam লক্ষণ নেই। তবে AI যাচাই হয়নি, কেনার আগে দাম নিজে দেখে নিন।",
                "reasons": ["একাধিক উৎস", "scam লক্ষণ নেই"], "risks": ["দাম এখনও যাচাই হয়নি"],
                "fit": "", "source": "rules"}
    return {"verdict": "wait", "confidence": 40, "scam_likelihood": "medium" if flags else "low",
            "matches_request": None,
            "summary": "এখনো যথেষ্ট প্রমাণ নেই। আরও channel-এ এলে বা দাম যাচাই হলে নিশ্চিত হওয়া যাবে।",
            "reasons": ["প্রমাণ কম"], "risks": ["দাম বা stock বদলে যেতে পারে"], "fit": "", "source": "rules"}
