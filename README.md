# OfferRadar

Amazon, Flipkart, Meesho-র offer আর আপনার Telegram channel/group-এর deal ২৪/৭ পড়ে, link ও scam যাচাই করে, layer-এ সাজিয়ে রাখে। Web app-এ Flipkart-ধাঁচের deal তালিকা, AI chat আর live status; আর কম দামে পণ্য পেলে Telegram-এ message।

পুরো পরিকল্পনা: [PLAN.md](PLAN.md)

## ১. দ্রুত চালু (কোনো key ছাড়াই demo)

Python ৩.১১+ লাগবে।

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # তারপর .env খুলে APP_TOKEN-এ একটা লম্বা পাসওয়ার্ড দিন
uvicorn app.main:app --port 8000 --workers 1
```

ব্রাউজারে `http://localhost:8000` খুলুন, `APP_TOKEN` দিয়ে ঢুকুন, **সেটিংস → demo ডেটা লোড করুন**। এতে UI, link যাচাই, scoring আর scam আটকানো দেখতে পাবেন। কোনো key না থাকলেও সিস্টেম নিয়ম দিয়ে চলে।

## ২. কোন key কোথা থেকে

| কী | কোথায় | কাজ |
|---|---|---|
| `GEMINI_API_KEY` | Google AI Studio | মূল AI |
| `AI2_API_KEY`, `AI2_BASE_URL`, `AI2_MODEL` | আপনার ২য় provider (OpenAI-ধাঁচের API হলে) | fallback + দ্বিতীয় মত |
| `TAVILY_API_KEY` | tavily.com | review/scam খোঁজা |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | my.telegram.org → API development tools | channel পড়া |
| `TELEGRAM_BOT_TOKEN` | Telegram-এ @BotFather | আপনাকে alert পাঠানো |

**কোনো key চ্যাটে পেস্ট করবেন না, GitHub-এ তুলবেন না।** `.env` শুধু আপনার সার্ভারে থাকবে (`.gitignore` এটা আটকায়)।

## ৩. Telegram channel পড়া চালু করা

আপনি যে channel/group-এ join করা আছেন সেগুলো আপনার নিজের account দিয়ে **শুধু পড়া** হয়। Bot লাগে না, admin হতে হয় না।

1. `my.telegram.org`-এ গিয়ে `api_id`, `api_hash` নিন, `.env`-এ বসান।
2. **নিজের কম্পিউটারে:** `pip install telethon` তারপর `python scripts/telegram_login.py`। ফোন নম্বর আর Telegram-এর OTP দিন।
3. যে `TELEGRAM_STRING_SESSION=...` লাইন ছাপা হবে, সেটা `.env`-এ বসান। এটা আপনার account-এর চাবি, কাউকে দেবেন না।
4. Channel-এ join আপনি নিজে Telegram app থেকে করুন, একটু একটু করে (একসঙ্গে অনেক join করলে Telegram সন্দেহ করে)।
5. কোন channel পড়া হবে তা `config/settings.yml`-এর `telegram.mode` (`all` বা `whitelist`) দিয়ে ঠিক করুন।

## ৪. Telegram-এ alert পাওয়া (bot)

1. @BotFather → `/newbot` → token নিয়ে `TELEGRAM_BOT_TOKEN`-এ বসান।
2. `TELEGRAM_PAIR_CODE`-এ একটা গোপন শব্দ দিন (যেমন `meghla-9981`)।
3. সার্ভার চালু করে আপনার bot-এ লিখুন: `/start meghla-9981`। এতে শুধু আপনার chat জোড়া লাগবে, অন্য কেউ পারবে না।
4. এরপর bot-এ সরাসরি লিখেও task দিতে পারবেন, যেমন "boat earbuds ৯০০ টাকার নিচে চাই"। `/tasks`, `/stop 3`, `/status` কাজ করে।

## ৫. ২৪/৭ চালানো

আপনার কম্পিউটার বন্ধ হলে সব থামবে, তাই একটা সার্ভার লাগবে যেটা ঘুমায় না (সস্তা VPS বা সবসময় চালু থাকা hosting)। Free hosting-এর অনেকগুলো কিছুক্ষণ পর ঘুমিয়ে যায়, তাই আগে নিয়ম দেখে নেবেন।

```bash
docker compose up -d --build
```

- `127.0.0.1:8000`-এ চলে। ফোন থেকে খুলতে সামনে https reverse proxy (Caddy/nginx) বসান, তারপর `.env`-এ `COOKIE_SECURE=1`।
- **একটাই worker** (Telegram পাঠক, AI বাজেট, SQLite এক process-এ থাকে)। worker বাড়াবেন না।
- `data/` ফোল্ডার backup রাখুন: database ও ছবি ওখানে।

## ৬. AI limit কীভাবে সামলানো হয়

`config/settings.yml`-এর `ai:` অংশে `rpm` (মিনিটে) ও `daily_total` (দিনে) বদলানো যায়। Gemini-র আসল limit model ভেদে আলাদা, তাই AI Studio-তে আপনার limit দেখে সংখ্যা মিলিয়ে নিন। "লাইভ" ট্যাবে আজকের ব্যবহার দেখা যায়। বিস্তারিত: [PLAN.md](PLAN.md) §৩।

## ৭. কী পরীক্ষা হয়েছে, কী হয়নি

- ✅ ৪১টি automated test পাস: link যাচাই (নকল domain, short link, SSRF-নিরাপদ), post পড়া, scoring, AI router (rate limit, বাজেট, fallback, priority), পুরো pipeline, task, chat।
- ✅ Web app-এর JavaScript-এর syntax পরীক্ষা।
- ⚠️ **আসল Telegram, আসল AI key আর FastAPI server এখানে চালিয়ে দেখা হয়নি** (বানানোর পরিবেশে ইন্টারনেট ছিল না)। প্রথমবার চালালে ছোটখাটো সমস্যা আসতে পারে। log-এ যা দেখাবে সেটা জানালে ঠিক করে দেব।
- ⚠️ Flipkart API adapter পরীক্ষামূলক, Amazon Creators API adapter এখনো নেই (`app/sources.py` দেখুন)।

## ৮. কোড কোথায়

```
app/main.py              API, login, live stream, background কাজ
app/telegram_listener.py channel পড়া (Telethon)
app/pipeline.py          post → parse → link → deal → score → verify
app/links.py             link allow-list, short link, normalize, probe
app/parser.py            নিয়মভিত্তিক post পড়া, scam লক্ষণ
app/ai/router.py         rate limit, বাজেট, priority, cache, fallback
app/tasks.py             "কম দামে চাই" task
app/notifier.py          Telegram bot (alert + দুই-মুখী chat)
app/chat.py              web/Telegram chat
web/                     UI (HTML/CSS/JS, কোনো বাইরের library ছাড়া)
tests/                   python -m unittest discover -s tests
```

## ৯. GitHub-এ তোলা

```bash
git clone https://github.com/outerrouter/outer-1.git
# এই প্রজেক্টের সব ফাইল ওই ফোল্ডারে কপি করুন (.env নয়!), তারপর:
cd outer-1
git add .
git status          # .env বা *.session যেন তালিকায় না থাকে
git commit -m "OfferRadar: first working version"
git push origin main
```

## নিরাপত্তা মনে রাখুন

- `.env` আর `TELEGRAM_STRING_SESSION` কারও সঙ্গে শেয়ার নয়।
- Repo public হলে `.env.example` ছাড়া কোনো secret ফাইল তোলা যাবে না।
- Web app একজন মালিকের জন্য, পাসওয়ার্ড লম্বা রাখুন।
