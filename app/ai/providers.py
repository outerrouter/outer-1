"""HTTP clients for the AI providers. Keys are sent in headers, never in URLs or logs."""
from __future__ import annotations

from .router import AuthError, ProviderError, RateLimited


def _check(resp) -> None:
    code = resp.status_code
    if code < 400:
        return
    if code == 429:
        try:
            retry = float(resp.headers.get("retry-after", 20))
        except ValueError:
            retry = 20.0
        raise RateLimited(retry)
    if code in (401, 403):
        raise AuthError(f"HTTP {code}")
    raise ProviderError(f"HTTP {code}")


class GeminiProvider:
    name = "gemini"

    def __init__(self, http, key: str, model: str):
        self.http, self.key, self.model = http, key, model

    async def generate(self, system: str, user: str, json_mode: bool = False, max_tokens: int = 700) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        config = {"temperature": 0.2, "maxOutputTokens": max_tokens}
        if json_mode:
            config["responseMimeType"] = "application/json"
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": config,
        }
        resp = await self.http.post(url, json=body, headers={"x-goog-api-key": self.key}, timeout=45)
        _check(resp)
        try:
            parts = resp.json()["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError("empty response") from exc


class OpenAICompatProvider:
    """Any provider that speaks the OpenAI chat-completions format (set AI2_BASE_URL + AI2_MODEL)."""

    name = "second"

    def __init__(self, http, key: str, base_url: str, model: str):
        self.http, self.key, self.model = http, key, model
        self.base_url = base_url.rstrip("/")

    async def generate(self, system: str, user: str, json_mode: bool = False, max_tokens: int = 700) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        resp = await self.http.post(f"{self.base_url}/chat/completions", json=body,
                                    headers={"Authorization": f"Bearer {self.key}"}, timeout=45)
        _check(resp)
        try:
            return resp.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError("empty response") from exc
