"""Link handling: extract, unshorten safely, allow-list, normalise to a product key, probe.

Safety rules (the server never opens a link it has not allow-listed):
  * only amazon.in / flipkart.com / meesho.com are accepted as final destinations
  * known URL shorteners are followed ONE hop at a time, reading only the redirect header
  * anything else (unknown domain, look-alike domain, user@host tricks) is blocked, never fetched
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urljoin, urlsplit

ALLOWED = {
    "amazon": ("amazon.in",),
    "flipkart": ("flipkart.com",),
    "meesho": ("meesho.com",),
}
BRAND_WORDS = ("amazon", "amzn", "flipkart", "fkrt", "meesho")
UA = {"User-Agent": "Mozilla/5.0 (compatible; OfferRadar/1.0; link-check)"}
URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.I)
_TRAIL = ".,;:!?)]}>*_~"
ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d|product|exec/obidos/ASIN)/([A-Z0-9]{10})(?:[/?#]|$)", re.I)


def extract_urls(text: str, extra=()) -> list[str]:
    seen: list[str] = []
    for raw in list(URL_RE.findall(text or "")) + list(extra or ()):
        url = raw.rstrip(_TRAIL)
        if url and url not in seen:
            seen.append(url)
    return seen


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def platform_of(host: str) -> str | None:
    for name, domains in ALLOWED.items():
        for d in domains:
            if host == d or host.endswith("." + d):
                return name
    return None


def is_shortener(host: str, shorteners) -> bool:
    return any(host == s or host.endswith("." + s) for s in shorteners)


def looks_like_brand(host: str) -> bool:
    """A host that is NOT allow-listed but imitates a shop (amazon-offers.xyz, xn-- tricks)."""
    if platform_of(host):
        return False
    return host.startswith("xn--") or ".xn--" in host or any(w in host for w in BRAND_WORDS)


def normalize(url: str) -> tuple[str | None, str | None, str]:
    """-> (platform, product id or None, canonical url without tracking parameters)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    platform = platform_of(host)
    if platform is None:
        return None, None, url
    path = parts.path or "/"
    query = parse_qs(parts.query)
    pid = None
    canonical = f"https://{host}{path}"
    if platform == "amazon":
        m = ASIN_RE.search(path)
        if m:
            pid = m.group(1).upper()
            canonical = f"https://www.amazon.in/dp/{pid}"
    elif platform == "flipkart":
        pid = (query.get("pid") or [None])[0]
        if not pid:
            m = re.search(r"/p/(itm[a-z0-9]+)", path, re.I)
            pid = m.group(1) if m else None
        if pid:
            pid = pid.upper()
            canonical = f"https://www.flipkart.com{path}?pid={pid}"
    elif platform == "meesho":
        m = re.search(r"/p/([a-z0-9]+)", path, re.I)
        if m:
            pid = m.group(1).lower()
            canonical = f"https://www.meesho.com/s/p/{pid}"
    return platform, pid, canonical


@dataclass
class LinkInfo:
    original: str
    url: str = ""
    platform: str | None = None
    pid: str | None = None
    key: str = ""
    canonical: str = ""
    state: str = "blocked"          # ok | blocked
    reason: str = ""
    chain: list = field(default_factory=list)


async def resolve(url: str, client, shorteners, max_hops: int = 5, timeout: float = 6.0):
    """Follow known shorteners hop by hop. -> (final_url, chain, error or None)."""
    chain, cur = [url], url
    for _ in range(max_hops + 1):
        parts = urlsplit(cur)
        if parts.scheme not in ("http", "https"):
            return cur, chain, "bad_scheme"
        if parts.username or parts.password:
            return cur, chain, "userinfo_trick"
        host = host_of(cur)
        if platform_of(host):
            return cur, chain, None
        if not is_shortener(host, shorteners):
            return cur, chain, "unknown_domain"
        try:
            async with client.stream("GET", cur, follow_redirects=False, timeout=timeout, headers=UA) as r:
                location = r.headers.get("location")
        except Exception:
            return cur, chain, "resolve_failed"
        if not location:
            return cur, chain, "no_redirect"
        cur = urljoin(cur, location)
        chain.append(cur)
    return cur, chain, "too_many_hops"


async def check_link(url: str, client, shorteners) -> LinkInfo:
    final, chain, err = await resolve(url, client, shorteners)
    host = host_of(final)
    info = LinkInfo(original=url, url=final, chain=chain)
    platform = platform_of(host)
    if platform is None or err in ("bad_scheme", "userinfo_trick"):
        info.reason = err or ("lookalike" if looks_like_brand(host) else "unknown_domain")
        if info.reason in ("unknown_domain", "no_redirect", "resolve_failed", "too_many_hops") and looks_like_brand(host):
            info.reason = "lookalike"
        return info
    platform, pid, canonical = normalize(final)
    info.platform, info.pid, info.canonical, info.state = platform, pid, canonical, "ok"
    if pid:
        info.key = f"{platform}:{pid}"
    else:
        digest = hashlib.sha1((host + urlsplit(canonical).path).encode()).hexdigest()[:12]
        info.key = f"{platform}:h{digest}"
    return info


class DomainThrottle:
    """At most one request per `gap` seconds per host (be polite to the shops)."""

    def __init__(self, gap: float = 2.0):
        self.gap, self.last = gap, {}
        self.lock = asyncio.Lock()

    async def wait(self, host: str) -> None:
        async with self.lock:
            now = time.monotonic()
            ready = self.last.get(host, 0.0) + self.gap
            if ready > now:
                await asyncio.sleep(ready - now)
            self.last[host] = time.monotonic()


async def probe(url: str, client, timeout: float = 6.0) -> str:
    """'dead' (404/410), 'ok' (2xx/3xx) or 'unknown'. Reads only the status line, stays on allow-listed hosts."""
    cur = url
    for _ in range(4):
        if not platform_of(host_of(cur)):
            return "unknown"
        try:
            async with client.stream("GET", cur, follow_redirects=False, timeout=timeout, headers=UA) as r:
                code, location = r.status_code, r.headers.get("location")
        except Exception:
            return "unknown"
        if code in (404, 410):
            return "dead"
        if 300 <= code < 400 and location:
            cur = urljoin(cur, location)
            continue
        return "ok" if 200 <= code < 300 else "unknown"
    return "unknown"
