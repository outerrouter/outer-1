"""FastAPI app: REST API + live events (SSE) + the web UI + all background workers."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .ai.providers import GeminiProvider, OpenAICompatProvider
from .ai.router import AIRouter, Slot
from .chat import ChatEngine
from .config import ROOT, load_settings
from .db import Database
from .demo import load_demo
from .events import Bus
from .notifier import Notifier
from .pipeline import Pipeline
from .sources import build_sources
from .tasks import TaskEngine
from .tavily import Tavily
from .telegram_listener import Listener
from .util import deal_out, tokenize

log = logging.getLogger("app")
WEB = ROOT / "web"
PLATFORMS = ("amazon", "flipkart", "meesho")


# ---------------------------------------------------------------------- background work
async def scheduler(app: FastAPI) -> None:
    pl: Pipeline = app.state.pipeline
    tick = 0
    while True:
        await asyncio.sleep(60)
        tick += 1
        try:
            if tick % 5 == 0:
                await pl.retry_pending()
            if tick % 10 == 0:
                pl.age_deals()
            if tick % 15 == 0 and pl.sources:
                keys = [r["key"] for r in pl.db.all(
                    "SELECT key FROM deals WHERE status IN ('live','free') AND platform IN (%s) ORDER BY score DESC LIMIT 10"
                    % ",".join("?" * len(pl.sources)), list(pl.sources))]
                for key in keys:
                    await pl.refresh_price(key)
                    await asyncio.sleep(2)
            if tick % 1440 == 0:
                pl.cleanup()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler step failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_settings()
    s = cfg.secrets
    if s["app_token_generated"]:
        log.warning("APP_TOKEN is not set in .env. Temporary login password for this run: %s", s["app_token"])
    db = Database(str(Path(cfg.data_dir) / "offerradar.db"))
    bus = Bus()
    http = httpx.AsyncClient(headers={"User-Agent": "OfferRadar/1.0"}, limits=httpx.Limits(max_connections=30))
    slots = []
    if s["gemini_key"]:
        slots.append(Slot(GeminiProvider(http, s["gemini_key"], cfg.ai.gemini_model), int(cfg.ai.rpm)))
    if s["ai2_key"] and cfg.ai.second_base_url and cfg.ai.second_model:
        slots.append(Slot(OpenAICompatProvider(http, s["ai2_key"], cfg.ai.second_base_url, cfg.ai.second_model), int(cfg.ai.rpm)))
    ai = AIRouter(db, slots, cfg)
    ai.start()
    tavily = Tavily(cfg, db, http)
    pipeline = Pipeline(cfg, db, bus, ai, http, tavily, build_sources(cfg, http))
    tasks = TaskEngine(db, bus)
    notifier = Notifier(cfg, db, http, bus)
    chat = ChatEngine(cfg, db, ai, pipeline, tasks)
    listener = Listener(cfg, pipeline, bus)
    pipeline.tasks, pipeline.notifier = tasks, notifier
    tasks.pipeline, tasks.notifier = pipeline, notifier
    notifier.pipeline, notifier.tasks, notifier.chat = pipeline, tasks, chat
    for name, value in dict(cfg=cfg, db=db, bus=bus, ai=ai, tavily=tavily, pipeline=pipeline, tasks=tasks,
                            notifier=notifier, chat=chat, listener=listener, login_tries=defaultdict(deque)).items():
        setattr(app.state, name, value)
    workers = [asyncio.create_task(c) for c in (pipeline.batcher(), listener.run(), notifier.poll(), scheduler(app))]
    log.info("%s is running. AI keys: %d, Telegram reader: %s, bot: %s", cfg.app_name, len(slots),
             "on" if listener.configured else "off", "on" if notifier.configured else "off")
    try:
        yield
    finally:
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await ai.stop()
        await http.aclose()


class SecurityHeaders:
    """Pure ASGI middleware (does not interfere with the live SSE stream)."""
    HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
        (b"content-security-policy", b"default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; "
                                     b"script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"),
    ]

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def wrapped(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + self.HEADERS
            await send(message)
        await self.app(scope, receive, wrapped)


app = FastAPI(title="OfferRadar", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SecurityHeaders)


# ---------------------------------------------------------------------- auth (single owner, cookie session)
def _sig(token: str) -> str:
    return hmac.new(token.encode(), b"offerradar-session-v1", hashlib.sha256).hexdigest()


async def auth(request: Request) -> None:
    token = request.app.state.cfg.secrets["app_token"]
    if not hmac.compare_digest(request.cookies.get("session", ""), _sig(token)):
        raise HTTPException(401, "login required")


class LoginIn(BaseModel):
    token: str = Field(max_length=200)


class ChatIn(BaseModel):
    text: str = Field(max_length=500)


class TaskIn(BaseModel):
    text: str = Field(min_length=2, max_length=200)
    target_price: float | None = Field(default=None, ge=0, le=10_000_000)
    platform: str | None = None


class WatchIn(BaseModel):
    target_price: float | None = Field(default=None, ge=0, le=10_000_000)


class ProfileIn(BaseModel):
    budget_max: float | None = Field(default=None, ge=0, le=10_000_000)
    interests: list[str] = Field(default_factory=list, max_length=20)


class IngestIn(BaseModel):
    text: str = Field(min_length=3, max_length=3000)
    channel: str = Field(default="Manual test", max_length=80)


@app.post("/api/login")
async def login(body: LoginIn, request: Request, response: Response):
    tries = request.app.state.login_tries[request.client.host if request.client else "?"]
    now = time.time()
    while tries and now - tries[0] > 600:
        tries.popleft()
    if len(tries) >= 8:
        raise HTTPException(429, "too many attempts, wait 10 minutes")
    cfg = request.app.state.cfg
    if not hmac.compare_digest(body.token, cfg.secrets["app_token"]):
        tries.append(now)
        raise HTTPException(401, "wrong password")
    response.set_cookie("session", _sig(cfg.secrets["app_token"]), httponly=True, samesite="lax",
                        secure=cfg.secrets["secure_cookie"], max_age=30 * 86400)
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie("session")
    return {"ok": True}


# ---------------------------------------------------------------------- read endpoints
@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/state", dependencies=[Depends(auth)])
async def state(request: Request):
    st = request.app.state
    return {
        "app_name": st.cfg.app_name,
        "stats": st.pipeline.stats(),
        "ai": st.ai.usage(),
        "tasks_open": len(st.tasks.list(include_closed=False)),
        "services": {
            "ai_keys": len(st.ai.slots), "tavily": st.tavily.enabled,
            "telegram_reader_configured": st.listener.configured, "telegram_reader_connected": st.listener.connected,
            "bot_configured": st.notifier.configured, "bot_paired": st.notifier.chat_id is not None,
            "price_apis": list(st.pipeline.sources),
        },
        "recent": list(st.bus.recent)[-40:],
        "profile": st.db.kv_get("profile", dict(st.cfg.profile)),
    }


SORTS = {"score": "score DESC, last_seen DESC", "new": "first_seen DESC", "discount": "discount DESC, score DESC",
         "price": "price IS NULL, price ASC"}
STATUS_GROUPS = {"live": ("live", "free"), "free": ("free",), "ended": ("stale", "dead"),
                 "all": ("live", "free", "stale", "dead")}


@app.get("/api/deals", dependencies=[Depends(auth)])
async def deals(request: Request, platform: str = "", status: str = "live", q: str = "", sort: str = "score",
                limit: int = 40, offset: int = 0):
    statuses = STATUS_GROUPS.get(status, STATUS_GROUPS["live"])
    sql = "SELECT * FROM deals WHERE status IN (%s)" % ",".join("?" * len(statuses))
    params: list = list(statuses)
    if platform in PLATFORMS:
        sql += " AND platform=?"
        params.append(platform)
    for word in tokenize(q)[:5]:
        sql += " AND LOWER(title) LIKE ? ESCAPE '\\'"
        params.append("%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    sql += " ORDER BY " + SORTS.get(sort, SORTS["score"]) + " LIMIT ? OFFSET ?"
    params += [max(1, min(limit, 100)), max(0, offset)]
    return [deal_out(r) for r in request.app.state.db.all(sql, params)]


@app.get("/api/deals/{deal_id}", dependencies=[Depends(auth)])
async def deal_detail(deal_id: int, request: Request):
    db = request.app.state.db
    row = db.one("SELECT * FROM deals WHERE id=?", (deal_id,))
    if not row:
        raise HTTPException(404, "deal not found")
    out = deal_out(row)
    out["history"] = db.all("SELECT ts, price, source FROM price_history WHERE deal_key=? ORDER BY ts LIMIT 100", (row["key"],))
    out["channels"] = db.all("SELECT s.channel AS name, s.ts AS ts, c.trust AS trust FROM deal_sources s "
                             "LEFT JOIN channels c ON c.name=s.channel WHERE s.deal_key=? ORDER BY s.ts", (row["key"],))
    return out


@app.post("/api/deals/{deal_id}/check", dependencies=[Depends(auth)])
async def deal_check(deal_id: int, request: Request):
    st = request.app.state
    row = st.db.one("SELECT key FROM deals WHERE id=?", (deal_id,))
    if not row:
        raise HTTPException(404, "deal not found")
    st.bus.publish("check", {"id": deal_id, "stage": "started"})
    verdict = await st.pipeline.verify_deal(row["key"], kind="chat")
    return {"verdict": verdict, "deal": deal_out(st.pipeline.get_deal(row["key"]))}


@app.post("/api/deals/{deal_id}/watch", dependencies=[Depends(auth)])
async def deal_watch(deal_id: int, body: WatchIn, request: Request):
    st = request.app.state
    row = st.db.one("SELECT * FROM deals WHERE id=?", (deal_id,))
    if not row:
        raise HTTPException(404, "deal not found")
    target = body.target_price if body.target_price is not None else (row["price"] or None)
    task = st.tasks.create(row["title"][:100], None, target, None, product_key=row["key"])
    await st.tasks.scan_existing(task["id"])
    return st.tasks.get(task["id"])


@app.get("/api/tasks", dependencies=[Depends(auth)])
async def list_tasks(request: Request):
    return request.app.state.tasks.list()


@app.post("/api/tasks", dependencies=[Depends(auth)])
async def add_task(body: TaskIn, request: Request):
    st = request.app.state
    kws = [w for w in tokenize(body.text) if w.isascii()] or tokenize(body.text)
    if not kws:
        raise HTTPException(400, "write the product name")
    platform = body.platform if body.platform in PLATFORMS else "any"
    task = st.tasks.create(body.text, kws[:6], body.target_price, platform)
    await st.tasks.scan_existing(task["id"])
    return st.tasks.get(task["id"])


@app.delete("/api/tasks/{task_id}", dependencies=[Depends(auth)])
async def cancel_task(task_id: int, request: Request):
    return {"ok": request.app.state.tasks.cancel(task_id)}


@app.post("/api/chat", dependencies=[Depends(auth)])
async def chat(body: ChatIn, request: Request):
    return await request.app.state.chat.handle(body.text)


@app.get("/api/chat/history", dependencies=[Depends(auth)])
async def chat_history(request: Request):
    return request.app.state.chat.history(30)


@app.get("/api/channels", dependencies=[Depends(auth)])
async def channels(request: Request):
    return request.app.state.db.all("SELECT name, posts, good, bad, trust FROM channels ORDER BY posts DESC LIMIT 200")


@app.post("/api/profile", dependencies=[Depends(auth)])
async def save_profile(body: ProfileIn, request: Request):
    profile = {"budget_max": body.budget_max, "interests": [i.strip()[:40] for i in body.interests if i.strip()]}
    request.app.state.db.kv_set("profile", profile)
    return profile


@app.post("/api/dev/ingest", dependencies=[Depends(auth)])
async def dev_ingest(body: IngestIn, request: Request):
    await request.app.state.pipeline.ingest({"channel": body.channel, "channel_id": body.channel,
                                             "msg_id": int(time.time() * 1000), "text": body.text, "no_probe": True})
    return {"ok": True}


@app.post("/api/dev/demo", dependencies=[Depends(auth)])
async def dev_demo(request: Request):
    return {"loaded": await load_demo(request.app.state.pipeline)}


@app.get("/api/stream", dependencies=[Depends(auth)])
async def stream(request: Request):
    bus: Bus = request.app.state.bus
    q = bus.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    if await request.is_disconnected():
                        break
                    continue
                yield f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/media/{name}", dependencies=[Depends(auth)])
async def media(name: str, request: Request):
    path = Path(request.app.state.cfg.data_dir) / "media" / Path(name).name
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/assets", StaticFiles(directory=str(WEB)), name="assets")
