"""Settings: non-secret values from config/settings.yml, secrets from environment / .env."""
from __future__ import annotations

import copy
import os
import secrets
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    "app_name": "OfferRadar",
    "data_dir": "data",
    "ai": {
        "rpm": 10,                    # keep below the provider's real per-minute limit (you said 10-14)
        "daily_total": 2000,          # total AI requests per day (all kinds together)
        "shares": {"parse": 500, "deep": 600, "chat": 500, "alert": 300, "reserve": 100},
        "cache_hours": 6,
        "gemini_model": "gemini-flash-latest",   # check the exact name in Google AI Studio
        "second_base_url": "",        # OpenAI-compatible base URL of your 2nd provider
        "second_model": "",
    },
    "tavily": {"monthly_cap": 900, "daily_cap": 30},
    "telegram": {"mode": "all", "allow": [], "deny": [], "save_images": True},
    "pipeline": {
        "batch_size": 10,
        "batch_wait_sec": 60,
        "max_text_chars": 900,
        "stale_hours": 12,
        "probe_links": True,
        "alert_min_score": 70,
        "free_alerts": True,
        "alerts_per_hour": 12,
        "retention_days": 7,
    },
    "profile": {"budget_max": None, "interests": []},
    "extra_shorteners": [],          # add your own shortener domains here (keeps the defaults below)
    "shorteners": [
        "amzn.to", "amzn.in", "a.co", "fkrt.it", "fkrt.cc", "fkrt.to", "bit.ly", "cutt.ly",
        "tinyurl.com", "t.ly", "rb.gy", "is.gd", "shorturl.at", "ekaro.in", "inr.deals", "bitli.in",
    ],
}


class Box(dict):
    """dict with attribute access (read-only convenience)."""

    def __getattr__(self, key):
        try:
            value = self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc
        return Box(value) if isinstance(value, dict) else value


def _merge(base: dict, extra: dict) -> dict:
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_settings(path: str | Path | None = None) -> Box:
    load_dotenv(ROOT / ".env")
    cfg = copy.deepcopy(DEFAULTS)
    cfg_path = Path(path or os.getenv("OUTER_CONFIG") or ROOT / "config" / "settings.yml")
    if cfg_path.exists():
        _merge(cfg, yaml.safe_load(cfg_path.read_text("utf-8")) or {})

    env = os.environ
    data_dir = Path(env.get("DATA_DIR") or cfg["data_dir"])
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg["data_dir"] = str(data_dir)

    if env.get("AI2_BASE_URL"):
        cfg["ai"]["second_base_url"] = env["AI2_BASE_URL"]
    if env.get("AI2_MODEL"):
        cfg["ai"]["second_model"] = env["AI2_MODEL"]
    if env.get("GEMINI_MODEL"):
        cfg["ai"]["gemini_model"] = env["GEMINI_MODEL"]

    api_id = env.get("TELEGRAM_API_ID", "").strip()
    app_token = env.get("APP_TOKEN", "").strip()
    generated = False
    if not app_token:
        app_token, generated = secrets.token_urlsafe(18), True
    cfg["secrets"] = {
        "app_token": app_token,
        "app_token_generated": generated,
        "tg_api_id": int(api_id) if api_id.isdigit() else 0,
        "tg_api_hash": env.get("TELEGRAM_API_HASH", "").strip(),
        "tg_string_session": env.get("TELEGRAM_STRING_SESSION", "").strip(),
        "bot_token": env.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "owner_chat_id": env.get("TELEGRAM_OWNER_CHAT_ID", "").strip(),
        "pair_code": env.get("TELEGRAM_PAIR_CODE", "").strip(),
        "gemini_key": env.get("GEMINI_API_KEY", "").strip(),
        "ai2_key": env.get("AI2_API_KEY", "").strip(),
        "tavily_key": env.get("TAVILY_API_KEY", "").strip(),
        "flipkart_id": env.get("FLIPKART_AFFILIATE_ID", "").strip(),
        "flipkart_token": env.get("FLIPKART_AFFILIATE_TOKEN", "").strip(),
        "secure_cookie": env.get("COOKIE_SECURE", "").strip() == "1",
    }
    return Box(cfg)
