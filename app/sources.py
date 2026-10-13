"""Optional official price sources. EXPERIMENTAL: endpoint and field names below must be checked against
the current Flipkart affiliate API documentation before you rely on them. A failure is harmless:
the pipeline simply keeps using the channel claims.

Amazon: the old PA-API 5 has been retired; the Creators API replaces it and needs an approved Associates
account. Add an `AmazonCreatorsSource` here (same `get(deal)` shape) once you have credentials.
"""
from __future__ import annotations

import logging

log = logging.getLogger("sources")


class FlipkartSource:
    def __init__(self, http, affiliate_id: str, token: str):
        self.http, self.aid, self.token = http, affiliate_id, token

    async def get(self, deal: dict) -> dict | None:
        if not deal.get("pid"):
            return None
        resp = await self.http.get("https://affiliate-api.flipkart.net/affiliate/1.0/product.json",
                                   params={"id": deal["pid"]},
                                   headers={"Fk-Affiliate-Id": self.aid, "Fk-Affiliate-Token": self.token}, timeout=20)
        if resp.status_code == 404:
            return {"available": False}
        if resp.status_code >= 400:
            log.warning("flipkart api HTTP %s", resp.status_code)
            return None
        info = (resp.json() or {}).get("productBaseInfoV1") or {}
        sell = (info.get("flipkartSpecialPrice") or info.get("flipkartSellingPrice") or {}).get("amount")
        mrp = (info.get("maximumRetailPrice") or {}).get("amount")
        images = info.get("imageUrls") or {}
        image = next(iter(images.values()), None) if isinstance(images, dict) else None
        return {"price": float(sell) if sell is not None else None, "mrp": float(mrp) if mrp is not None else None,
                "available": info.get("inStock", True), "image": image}


def build_sources(cfg, http) -> dict:
    s = cfg.secrets
    out = {}
    if s["flipkart_id"] and s["flipkart_token"]:
        out["flipkart"] = FlipkartSource(http, s["flipkart_id"], s["flipkart_token"])
    return out
