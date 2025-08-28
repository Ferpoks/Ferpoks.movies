# -*- coding: utf-8 -*-
"""
Telegram Movies & Series Bot (Arabic) – OMDb + Trakt + Watchmode
-----------------------------------------------------------------
✔ python-telegram-bot v21.x (async, polling)
✔ Reads .env via python-dotenv
✔ Search & posters via OMDb
✔ Calendars (today/week) via Trakt (Client ID only)
✔ Where-to-watch (region-aware) via Watchmode (optional)

Quick start
===========
1) Create .env next to this file:

BOT_TOKEN=123:ABC
OMDB_API_KEY=omdb_xxx
TRAKT_CLIENT_ID=trakt_xxx
WATCHMODE_API_KEY=watchmode_xxx   # optional
REGION=SA                         # default SA

2) Install & run:
   pip install python-telegram-bot~=21.6 httpx python-dotenv
   python3 telegram_movies_bot.py

Render (Background Worker)
==========================
- Build:    pip install -r requirements.txt
- Start:    python3 telegram_movies_bot.py
- Add the same env vars in the dashboard.

Notes
=====
- If you don't set WATCHMODE_API_KEY, the bot works but without the
  "📺 أماكن المشاهدة" button and /platform menu.
- If you don't set TRAKT_CLIENT_ID, /today and /week will say it's required.
- Keep messages short; this is a minimal MVP ready to extend.
"""

from __future__ import annotations
import os
import logging
from typing import Dict, Any, List, Optional

from dotenv import load_dotenv
from datetime import datetime, timezone
import httpx
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ---------------------------------------------------------------
# Env & logging
# ---------------------------------------------------------------
load_dotenv()  # read .env if present
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("mvbot")

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
OMDB_API_KEY = (os.getenv("OMDB_API_KEY") or "").strip()
TRAKT_CLIENT_ID = (os.getenv("TRAKT_CLIENT_ID") or "").strip()
WATCHMODE_API_KEY = (os.getenv("WATCHMODE_API_KEY") or "").strip()
REGION = (os.getenv("REGION") or "SA").strip().upper() or "SA"

if not BOT_TOKEN:
    raise SystemExit("[FATAL] BOT_TOKEN is missing. Put it in .env or environment.")
if not OMDB_API_KEY:
    raise SystemExit("[FATAL] OMDB_API_KEY is missing. Get it from omdbapi.com and set it.")

OMDB = "https://www.omdbapi.com/"

# Helper: today's date in UTC for Trakt
def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()

# single shared HTTP client
_client: Optional[httpx.AsyncClient] = None

def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=20)
    return _client

# ---------------------------------------------------------------
# OMDb helpers
# ---------------------------------------------------------------
async def omdb_search(q: str) -> List[Dict[str, Any]]:
    """Search movies/series by text; returns short list."""
    params = {"apikey": OMDB_API_KEY, "s": q, "type": "", "r": "json"}
    r = await client().get(OMDB, params=params)
    r.raise_for_status()
    js = r.json() or {}
    return js.get("Search") or []

async def omdb_by_id(imdb_id: str) -> Dict[str, Any]:
    """Fetch details/poster/plot by IMDb ID."""
    params = {"apikey": OMDB_API_KEY, "i": imdb_id, "plot": "short", "r": "json"}
    r = await client().get(OMDB, params=params)
    r.raise_for_status()
    return r.json() or {}

# ---------------------------------------------------------------
# Trakt helpers (calendars)
# ---------------------------------------------------------------
async def trakt_calendar(start: str, days: int, kind: str = "shows") -> List[Dict[str, Any]]:
    """kind in {"shows","movies"}. Requires TRAKT_CLIENT_ID only."""
    if not TRAKT_CLIENT_ID:
        return []
    headers = {
        "trakt-api-key": TRAKT_CLIENT_ID,
        "trakt-api-version": "2",
        "Content-Type": "application/json",
    }
    url = f"https://api.trakt.tv/calendars/all/{kind}/{start}/{days}"
    r = await client().get(url, headers=headers)
    r.raise_for_status()
    return r.json()

# ---------------------------------------------------------------
# Watchmode helpers (optional)
# ---------------------------------------------------------------
async def watchmode_search_by_imdb(imdb_id: str) -> Optional[int]:
    if not WATCHMODE_API_KEY:
        return None
    q_params = {"apiKey": WATCHMODE_API_KEY, "search_field": "imdb_id", "search_value": imdb_id}
    r = await client().get("https://api.watchmode.com/v1/search/", params=q_params)
    r.raise_for_status()
    res = (r.json() or {}).get("title_results") or []
    return (res[0].get("id") if res else None)

async def watchmode_sources_by_imdb(imdb_id: str) -> List[Dict[str, Any]]:
    if not WATCHMODE_API_KEY:
        return []
    wm_id = await watchmode_search_by_imdb(imdb_id)
    if not wm_id:
        return []
    params = {"apiKey": WATCHMODE_API_KEY, "regions": REGION}
    r = await client().get(f"https://api.watchmode.com/v1/title/{wm_id}/sources/", params=params)
    r.raise_for_status()
    keep = []
    for s in r.json():
        if s.get("region") == REGION and s.get("type") in {"sub", "free", "rent", "buy"}:
            keep.append({
                "name": s.get("name"),
                "type": s.get("type"),  # sub/free/rent/buy
                "web_url": s.get("web_url"),
            })
    return keep[:12]

async def watchmode_sources_list() -> List[Dict[str, Any]]:
    if not WATCHMODE_API_KEY:
        return []
    r = await client().get("https://api.watchmode.com/v1/sources/", params={"apiKey": WATCHMODE_API_KEY})
    r.raise_for_status()
    return r.json() or []

async def watchmode_titles_by_source_name(source_name: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Return popular titles on a given platform name in selected REGION.
    Tries both 'source_ids/regions' and 'source_id/region' flavors to avoid 400s.
    """
    if not WATCHMODE_API_KEY:
        return []
    sources = await watchmode_sources_list()
    sid = None
    for s in sources:
        if s.get("name", "").lower() == source_name.lower():
            sid = s.get("id")
            break
    if not sid:
        return []
    # Try plural params first
    params = {
        "apiKey": WATCHMODE_API_KEY,
        "source_ids": sid,
        "regions": REGION,
        "types": "movie,tv_series",
        "sort_by": "popularity_desc",
        "limit": limit,
    }
    try:
        r = await client().get("https://api.watchmode.com/v1/list-titles/", params=params)
        r.raise_for_status()
        return r.json().get("titles") or []
    except httpx.HTTPStatusError:
        try:
            params = {
                "apiKey": WATCHMODE_API_KEY,
                "source_id": sid,
                "region": REGION,
                "types": "movie,tv_series",
                "sort_by": "popularity_desc",
                "limit": limit,
            }
            r2 = await client().get("https://api.watchmode.com/v1/list-titles/", params=params)
            r2.raise_for_status()
            return r2.json().get("titles") or []
        except Exception:
            return []

# ---------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------
PLATFORMS = ["Netflix", "Prime Video", "Disney+", "Apple TV+", "OSN+", "Shahid"]

def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 اليوم", callback_data="MENU:today"), InlineKeyboardButton("📅 هذا الأسبوع", callback_data="MENU:week")],
        [InlineKeyboardButton("🔎 بحث", switch_inline_query_current_chat="")],
        [InlineKeyboardButton("📺 حسب المنصة", callback_data="MENU:platform")],
    ])

def platforms_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(p, callback_data=f"PLAT:{p}")] for p in PLATFORMS]
    rows.append([InlineKeyboardButton("⬅︎ رجوع", callback_data="MENU:home")])
    return InlineKeyboardMarkup(rows)

# ---------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------
def fmt_item_omdb(full: Dict[str, Any]) -> str:
    title = full.get("Title") or "—"
    year = full.get("Year") or "—"
    typ = (full.get("Type") or "").lower()
    plot = (full.get("Plot") or "").strip()
    if len(plot) > 240:
        plot = plot[:240] + "…"
    return f"*{title}* ({year})\nالنوع: `{typ}`\n{plot}"

# ---------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(
        "👋 أهلاً! بوت متابعة جديد الأفلام والمسلسلات.\n\n"
        "• /today — جديد اليوم\n• /week — هذا الأسبوع\n• /search اسم — بحث سريع\n• /platform — حسب المنصّة (لو فعلنا Watchmode)\n",
        reply_markup=menu_kb(),
    )

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message("pong ✅")

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = " ".join(context.args) if context.args else (
        update.message.text.split(maxsplit=1)[-1] if update.message and update.message.text.startswith("/search") else (update.message.text if update.message else "")
    )
    if not q:
        await update.effective_chat.send_message("اكتب: /search اسم الفيلم أو المسلسل")
        return
    try:
        rows = await omdb_search(q)
    except Exception as e:
        log.exception("omdb_search failed")
        await update.effective_chat.send_message("تعذر الاتصال بمصدر البحث حالياً.")
        return

    if not rows:
        await update.effective_chat.send_message("لم نجد نتائج.")
        return

    for it in rows[:6]:
        imdb_id = it.get("imdbID")
        try:
            full = await omdb_by_id(imdb_id)
        except Exception:
            continue
        poster = full.get("Poster") if full.get("Poster") and full.get("Poster") != "N/A" else None
        caption = fmt_item_omdb(full)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📺 أماكن المشاهدة", callback_data=f"SRC_IMDB:{imdb_id}")]]) if WATCHMODE_API_KEY else None
        if poster:
            await update.effective_chat.send_photo(photo=poster, caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            await update.effective_chat.send_message(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action = q.data.split(":", 1)[1]
    if action == "today":
        await show_today(q)
    elif action == "week":
        await show_week(q)
    elif action == "platform":
        await q.edit_message_text("اختر منصة:", reply_markup=platforms_kb())
    elif action == "home":
        await q.edit_message_text("القائمة الرئيسية:", reply_markup=menu_kb())

async def show_today(q):
    start = _today_iso()
    lines: List[str] = []
    try:
        shows = await trakt_calendar(start, 1, kind="shows") if TRAKT_CLIENT_ID else []
        movies = await trakt_calendar(start, 1, kind="movies") if TRAKT_CLIENT_ID else []
    except Exception:
        shows, movies = [], []
    for s in shows[:8]:
        try:
            lines.append(f"📺 {s['first_aired'][:10]} — {s['show']['title']} S{s['episode']['season']}E{s['episode']['number']}")
        except Exception:
            pass
    for m in movies[:8]:
        try:
            lines.append(f"🎬 {m['released']} — {m['movie']['title']}")
        except Exception:
            pass
    if not lines:
        await q.edit_message_text("*اليوم*
تعذر جلب تقاويم Trakt حاليًا. جرّب لاحقًا.", parse_mode=ParseMode.MARKDOWN, reply_markup=menu_kb())
        return
    await q.edit_message_text("*اليوم*
" + "
".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=menu_kb())

async def show_week(q):
    start = _today_iso()
    lines: List[str] = []
    try:
        shows = await trakt_calendar(start, 7, kind="shows") if TRAKT_CLIENT_ID else []
        movies = await trakt_calendar(start, 7, kind="movies") if TRAKT_CLIENT_ID else []
    except Exception:
        shows, movies = [], []
    for s in shows[:10]:
        try:
            lines.append(f"📺 {s['first_aired'][:10]} — {s['show']['title']} S{s['episode']['season']}E{s['episode']['number']}")
        except Exception:
            pass
    for m in movies[:10]:
        try:
            lines.append(f"🎬 {m['released']} — {m['movie']['title']}")
        except Exception:
            pass
    if not lines:
        await q.edit_message_text("*هذا الأسبوع*
تعذر جلب تقاويم Trakt حاليًا. جرّب لاحقًا.", parse_mode=ParseMode.MARKDOWN, reply_markup=menu_kb())
        return
    await q.edit_message_text("*هذا الأسبوع*
" + "
".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=menu_kb())

async def on_sources_imdb(update: Update, context: ContextTypes.DEFAULT_TYPE):(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    imdb_id = q.data.split(":", 1)[1]
    if not WATCHMODE_API_KEY:
        await q.edit_message_text("ميزة أماكن المشاهدة غير مفعّلة حالياً.")
        return
    src = await watchmode_sources_by_imdb(imdb_id)
    if not src:
        await q.edit_message_text("لم نعثر على منصات متاحة في منطقتك حالياً.")
        return
    m = []
    for s in src:
        typ = {"sub": "اشتراك", "free": "مجاني", "rent": "إيجار", "buy": "شراء"}.get(s["type"], s["type"])
        m.append(f"• {s['name']} — {typ}\n{s['web_url']}")
    await q.edit_message_text("*أماكن المشاهدة (" + REGION + ")*\n" + "\n".join(m), parse_mode=ParseMode.MARKDOWN, reply_markup=menu_kb())

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    class Q:
        def __init__(self, msg): self.message = msg
        async def answer(self): pass
        async def edit_message_text(self, *a, **kw):
            await update.effective_chat.send_message(*a, **kw)
    await show_today(Q(update.message))

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    class Q:
        def __init__(self, msg): self.message = msg
        async def answer(self): pass
        async def edit_message_text(self, *a, **kw):
            await update.effective_chat.send_message(*a, **kw)
    await show_week(Q(update.message))

async def cmd_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WATCHMODE_API_KEY:
        await update.effective_chat.send_message("هذه الميزة تعتمد على Watchmode. أضِف WATCHMODE_API_KEY لتفعيلها.")
        return
    await update.effective_chat.send_message("اختر منصة:", reply_markup=platforms_kb())

async def on_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, name = q.data.split(":", 1)
    if not WATCHMODE_API_KEY:
        await q.edit_message_text("لم يتم تفعيل Watchmode بعد.")
        return
    try:
        titles = await watchmode_titles_by_source_name(name, limit=12)
    except Exception:
        titles = []
    if not titles:
        await q.edit_message_text("لا توجد نتائج حالياً لهذه المنصة في منطقتك أو تعذّر الوصول للمزود.", parse_mode=ParseMode.MARKDOWN, reply_markup=menu_kb())
        return
    lines = []
    for t in titles:
        nm = t.get("title") or "—"
        ty = t.get("type") or "—"
        yr = t.get("year") or "—"
        lines.append(f"• {nm} ({yr}) — {ty}")
    await q.edit_message_text(f"*الأبرز على {name}*
" + "
".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=menu_kb())

# Error handler for visibility in logs
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error: %s", context.error)

# ---------------------------------------------------------------
# App builder
# ---------------------------------------------------------------
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "ابدأ"),
        BotCommand("search", "بحث"),
        BotCommand("today", "جديد اليوم"),
        BotCommand("week", "هذا الأسبوع"),
        BotCommand("platform", "حسب المنصة"),
        BotCommand("ping", "اختبار"),
    ])

def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_error_handler(on_error)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("platform", cmd_platform))

    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^MENU:"))
    app.add_handler(CallbackQueryHandler(on_sources_imdb, pattern=r"^SRC_IMDB:"))
    app.add_handler(CallbackQueryHandler(on_platform, pattern=r"^PLAT:"))

    # any plain text (not a command) → treat as quick search
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_search))
    return app

if __name__ == "__main__":
    log.info("[bot] starting… REGION=%s", REGION)
    build_app().run_polling(drop_pending_updates=True)
