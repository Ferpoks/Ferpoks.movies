# -*- coding: utf-8 -*-
"""
بوت تيليجرام احترافي وخفيف لعرض قوائم أفلام/مسلسلات مع فحص صحة الخدمات.
- يعتمد أساسًا على Trakt (قوائم رائجة/أكثر مشاهدة أسبوعيًا)
- يدعم فحص الحالة للأمزِدَة (Trakt/OMDb/Watchmode)
- معاملات آمنة، إعادة محاولات تلقائية، وتحرير رسائل بدون أخطاء "Message is not modified"

الأوامر:
  /start     → القائمة الرئيسية
  /movies    → أفلام رائجة الآن
  /shows     → مسلسلات رائجة الآن
  /topweek   → الأكثر مشاهدة هذا الأسبوع (أفلام + مسلسلات)
  /status    → فحص صحة المفاتيح والخدمات
  /ping      → اختبار سريع

بيئة التشغيل (Render → Environment):
  BOT_TOKEN=توكن_تيليجرام (مطلوب)
  TRAKT_CLIENT_ID=معرّف تطبيق Trakt (مطلوب)
  OMDB_API_KEY=مفتاح OMDb (اختياري للفحص)
  WATCHMODE_API_KEY=مفتاح Watchmode (اختياري للفحص)

Render (Background Worker):
  Build: pip install -r requirements.txt
  Start: python3 bot_pro.py

محليًا:
  pip install python-telegram-bot==21.6 httpx==0.27.0 python-dotenv==1.0.1
  BOT_TOKEN=... TRAKT_CLIENT_ID=... python3 bot_pro.py
"""
from __future__ import annotations
import os, logging, asyncio, time
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import httpx
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ===================== الإعداد العام =====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("probot")

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
TRAKT_CLIENT_ID = (os.getenv("TRAKT_CLIENT_ID") or "").strip()
OMDB_API_KEY = (os.getenv("OMDB_API_KEY") or "").strip()
WATCHMODE_API_KEY = (os.getenv("WATCHMODE_API_KEY") or "").strip()
REGION = (os.getenv("REGION") or "SA").strip().upper()

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN مفقود")
if not TRAKT_CLIENT_ID:
    raise SystemExit("TRAKT_CLIENT_ID مفقود")

# HTTP client مع حدود واتصالات دائمة
_http: Optional[httpx.AsyncClient] = None

def http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(
            timeout=20,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _http

# إعادة محاولات تلقائية بسيطة
RETRY_STATUSES = {429, 500, 502, 503, 504}
async def get_json(url: str, *, headers: Dict[str, str] | None = None, params: Dict[str, Any] | None = None, retries: int = 3) -> Any:
    for attempt in range(retries):
        try:
            r = await http().get(url, headers=headers, params=params)
            if r.status_code in RETRY_STATUSES and attempt + 1 < retries:
                await asyncio.sleep(0.6 * (2 ** attempt))
                continue
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code in RETRY_STATUSES and attempt + 1 < retries:
                await asyncio.sleep(0.6 * (2 ** attempt))
                continue
            raise

# ===================== Trakt API =====================
TRAKT_HEADERS = {
    "trakt-api-version": "2",
    "trakt-api-key": TRAKT_CLIENT_ID,
}

async def trakt(path: str, params: Dict[str, Any] | None = None) -> Any:
    return await get_json(f"https://api.trakt.tv{path}", headers=TRAKT_HEADERS, params=params or {})

async def trending_movies(limit: int = 10):
    return await trakt("/movies/trending", {"limit": limit})

async def trending_shows(limit: int = 10):
    return await trakt("/shows/trending", {"limit": limit})

async def watched_weekly_movies(limit: int = 10):
    return await trakt("/movies/watched/weekly", {"limit": limit})

async def watched_weekly_shows(limit: int = 10):
    return await trakt("/shows/watched/weekly", {"limit": limit})

# ===================== Watchmode (اختياري للفحص) =====================
async def watchmode_sources() -> Any:
    if not WATCHMODE_API_KEY:
        return None
    return await get_json("https://api.watchmode.com/v1/sources/", params={"apiKey": WATCHMODE_API_KEY})

# ===================== أدوات تنسيق + واجهة =====================

def _fmt_movie(m: Dict[str, Any], i: int) -> str:
    mv = m.get("movie", {}) if "movie" in m else m
    t = mv.get("title") or "—"
    y = mv.get("year") or "—"
    imdb = (mv.get("ids") or {}).get("imdb")
    link = f"\nhttps://www.imdb.com/title/{imdb}/" if imdb else ""
    return f"{i}. 🎬 *{t}* ({y}){link}"


def _fmt_show(s: Dict[str, Any], i: int) -> str:
    sh = s.get("show", {}) if "show" in s else s
    t = sh.get("title") or "—"
    y = sh.get("year") or "—"
    imdb = (sh.get("ids") or {}).get("imdb")
    link = f"\nhttps://www.imdb.com/title/{imdb}/" if imdb else ""
    return f"{i}. 📺 *{t}* ({y}){link}"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def fmt_block(title: str, lines: List[str]) -> str:
    if not lines:
        body = "لا توجد عناصر لعرضها الآن."
    else:
        body = "\n\n".join(lines)
    return f"*{title}*\nآخر تحديث: `{_stamp()}`\n\n{body}"


def menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 أفلام رائجة", callback_data="L:trend_movies"),
         InlineKeyboardButton("📺 مسلسلات رائجة", callback_data="L:trend_shows")],
        [InlineKeyboardButton("🔥 Top الأسبوع (أفلام)", callback_data="L:week_movies"),
         InlineKeyboardButton("🔥 Top الأسبوع (مسلسلات)", callback_data="L:week_shows")],
        [InlineKeyboardButton("🔧 حالة الخدمات", callback_data="L:status")],
    ])

async def safe_edit(q, text: str, **kw):
    try:
        await q.edit_message_text(text, **kw)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            # أرسل رسالة جديدة بدل التحرير
            await q.message.reply_text(text, **kw)
        else:
            raise

# ===================== Handlers =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(
        "👋 أهلاً! اختر قائمة مما يلي:", reply_markup=menu())

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message("pong ✅")

async def cmd_movies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        items = await trending_movies(10)
        lines = [_fmt_movie(m, i+1) for i, m in enumerate(items)]
        await update.effective_chat.send_message(fmt_block("أفلام رائجة الآن", lines), parse_mode=ParseMode.MARKDOWN, reply_markup=menu())
    except Exception:
        await update.effective_chat.send_message("تعذّر جلب القائمة حالياً.")

async def cmd_shows(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        items = await trending_shows(10)
        lines = [_fmt_show(s, i+1) for i, s in enumerate(items)]
        await update.effective_chat.send_message(fmt_block("مسلسلات رائجة الآن", lines), parse_mode=ParseMode.MARKDOWN, reply_markup=menu())
    except Exception:
        await update.effective_chat.send_message("تعذّر جلب القائمة حالياً.")

async def cmd_topweek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        mv = await watched_weekly_movies(5)
        sh = await watched_weekly_shows(5)
        lines = [_fmt_movie(m, i+1) for i, m in enumerate(mv)] + [""] + [_fmt_show(s, i+1) for i, s in enumerate(sh)]
        await update.effective_chat.send_message(fmt_block("الأكثر مشاهدة هذا الأسبوع", lines), parse_mode=ParseMode.MARKDOWN, reply_markup=menu())
    except Exception:
        await update.effective_chat.send_message("تعذّر جلب القائمة حالياً.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msgs = []
    # Trakt
    try:
        _ = await trending_movies(1)
        msgs.append("• Trakt: ✅")
    except Exception as e:
        msgs.append("• Trakt: ❌")
    # OMDb
    if OMDB_API_KEY:
        try:
            js = await get_json("https://www.omdbapi.com/", params={"apikey": OMDB_API_KEY, "s": "inception"})
            ok = bool(js and js.get("Response") == "True")
            msgs.append(f"• OMDb: {'✅' if ok else '⚠️'}")
        except Exception:
            msgs.append("• OMDb: ❌")
    # Watchmode
    if WATCHMODE_API_KEY:
        try:
            _ = await watchmode_sources()
            msgs.append("• Watchmode: ✅")
        except Exception:
            msgs.append("• Watchmode: ❌")
    if REGION:
        msgs.append(f"• REGION: `{REGION}`")
    await update.effective_chat.send_message("\n".join(msgs), parse_mode=ParseMode.MARKDOWN)

async def on_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    code = q.data
    try:
        if code == "L:trend_movies":
            items = await trending_movies(10)
            lines = [_fmt_movie(m, i+1) for i, m in enumerate(items)]
            await safe_edit(q, fmt_block("أفلام رائجة الآن", lines), parse_mode=ParseMode.MARKDOWN, reply_markup=menu())
        elif code == "L:trend_shows":
            items = await trending_shows(10)
            lines = [_fmt_show(s, i+1) for i, s in enumerate(items)]
            await safe_edit(q, fmt_block("مسلسلات رائجة الآن", lines), parse_mode=ParseMode.MARKDOWN, reply_markup=menu())
        elif code == "L:week_movies":
            items = await watched_weekly_movies(10)
            lines = [_fmt_movie(m, i+1) for i, m in enumerate(items)]
            await safe_edit(q, fmt_block("الأكثر مشاهدة هذا الأسبوع (أفلام)", lines), parse_mode=ParseMode.MARKDOWN, reply_markup=menu())
        elif code == "L:week_shows":
            items = await watched_weekly_shows(10)
            lines = [_fmt_show(s, i+1) for i, s in enumerate(items)]
            await safe_edit(q, fmt_block("الأكثر مشاهدة هذا الأسبوع (مسلسلات)", lines), parse_mode=ParseMode.MARKDOWN, reply_markup=menu())
        elif code == "L:status":
            # أعد استخدام منطق /status
            await cmd_status(update, context)
    except httpx.HTTPStatusError:
        await safe_edit(q, "تعذّر جلب القائمة من المزود الآن.", reply_markup=menu())
    except Exception:
        await safe_edit(q, "حدث خطأ غير متوقع.", reply_markup=menu())

# ===================== التطبيق =====================
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "ابدأ"),
        BotCommand("movies", "أفلام رائجة"),
        BotCommand("shows", "مسلسلات رائجة"),
        BotCommand("topweek", "Top الأسبوع"),
        BotCommand("status", "حالة الخدمات"),
        BotCommand("ping", "اختبار"),
    ])

def build() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("movies", cmd_movies))
    app.add_handler(CommandHandler("shows", cmd_shows))
    app.add_handler(CommandHandler("topweek", cmd_topweek))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_buttons, pattern=r"^L:"))
    # أي نص → افتح القائمة
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_start))
    return app

if __name__ == "__main__":
    log.info("[bot] starting — professional lists bot")
    build().run_polling(drop_pending_updates=True)
