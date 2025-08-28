# -*- coding: utf-8 -*-
"""
بوت تيليجرام يرسل *قوائم* جاهزة للأفلام والمسلسلات (بدون بحث)،
مبني على Trakt فقط لتقليل الأعطال.

الأوامر:
- /start      → قائمة رئيسية
- /list       → اختيارات القوائم
- /movies     → أفلام رائجة الآن
- /shows      → مسلسلات رائجة الآن
- /topweek    → الأكثر مشاهدة هذا الأسبوع (أفلام + مسلسلات)
- /ping       → اختبار سريع

البيئة المطلوبة (Render → Environment):
BOT_TOKEN=توكن_تيليجرام
TRAKT_CLIENT_ID=مفتاح_التطبيق_من_Trakt

تشغيل محلي:
  pip install python-telegram-bot==21.6 httpx==0.27.0
  python3 bot.py

Render (Background Worker):
  Build: pip install -r requirements.txt
  Start: python3 bot.py
"""
from __future__ import annotations
import os, logging, asyncio, time
from typing import List, Dict, Any, Optional

import httpx
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ===== إعدادات =====
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("listsbot")

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
TRAKT_CLIENT_ID = (os.getenv("TRAKT_CLIENT_ID") or "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN مفقود")
if not TRAKT_CLIENT_ID:
    raise SystemExit("TRAKT_CLIENT_ID مفقود")

HTTP_TIMEOUT = 20
_client: Optional[httpx.AsyncClient] = None

def http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
    return _client

TRAKT_HEADERS = {
    "trakt-api-version": "2",
    "trakt-api-key": TRAKT_CLIENT_ID,
}

# ===== مصادر Trakt (قوائم مستقرة) =====
async def trakt_get(path: str, params: Dict[str, Any] | None = None) -> Any:
    url = f"https://api.trakt.tv{path}"
    r = await http().get(url, headers=TRAKT_HEADERS, params=params or {})
    r.raise_for_status()
    return r.json()

async def trending_movies(limit: int = 10) -> List[Dict[str, Any]]:
    js = await trakt_get("/movies/trending", {"limit": limit})
    return js or []

async def trending_shows(limit: int = 10) -> List[Dict[str, Any]]:
    js = await trakt_get("/shows/trending", {"limit": limit})
    return js or []

async def watched_weekly_movies(limit: int = 10) -> List[Dict[str, Any]]:
    # الأكثر مشاهدة خلال الأسبوع (غالبًا مستقر)
    js = await trakt_get("/movies/watched/weekly", {"limit": limit})
    return js or []

async def watched_weekly_shows(limit: int = 10) -> List[Dict[str, Any]]:
    js = await trakt_get("/shows/watched/weekly", {"limit": limit})
    return js or []

# ===== تنسيق القوائم =====

def _fmt_line_movie(m: Dict[str, Any], idx: int) -> str:
    mv = m.get("movie", {}) if "movie" in m else m
    t = mv.get("title") or "—"
    y = mv.get("year") or "—"
    return f"{idx}. 🎬 *{t}* ({y})"


def _fmt_line_show(s: Dict[str, Any], idx: int) -> str:
    sh = s.get("show", {}) if "show" in s else s
    t = sh.get("title") or "—"
    y = sh.get("year") or "—"
    return f"{idx}. 📺 *{t}* ({y})"


def fmt_list(title: str, lines: List[str]) -> str:
    stamp = time.strftime("%H:%M:%S")
    body = "\n".join(lines) if lines else "لا توجد عناصر لعرضها الآن."
    return f"*{title}*\nآخر تحديث: `{stamp}`\n\n{body}"

# ===== لوحة الأزرار =====

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 أفلام رائجة", callback_data="L:trend_movies"),
         InlineKeyboardButton("📺 مسلسلات رائجة", callback_data="L:trend_shows")],
        [InlineKeyboardButton("🔥 Top أسبوع (أفلام)", callback_data="L:week_movies"),
         InlineKeyboardButton("🔥 Top أسبوع (مسلسلات)", callback_data="L:week_shows")],
    ])

# ===== Handlers =====

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(
        "👋 أهلاً! هذا البوت يرسل *قوائم* أفلام/مسلسلات.",
        reply_markup=main_menu(), parse_mode=ParseMode.MARKDOWN)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message("pong ✅")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message("اختر قائمة:", reply_markup=main_menu())

async def cmd_movies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        items = await trending_movies()
        lines = [_fmt_line_movie(m, i+1) for i, m in enumerate(items)]
        await update.effective_chat.send_message(fmt_list("أفلام رائجة الآن", lines), parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu())
    except Exception:
        await update.effective_chat.send_message("تعذّر جلب القائمة حالياً.")

async def cmd_shows(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        items = await trending_shows()
        lines = [_fmt_line_show(s, i+1) for i, s in enumerate(items)]
        await update.effective_chat.send_message(fmt_list("مسلسلات رائجة الآن", lines), parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu())
    except Exception:
        await update.effective_chat.send_message("تعذّر جلب القائمة حالياً.")

async def cmd_topweek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        mv = await watched_weekly_movies(5)
        sh = await watched_weekly_shows(5)
        lines = [_fmt_line_movie(m, i+1) for i, m in enumerate(mv)] + [""] + [_fmt_line_show(s, i+1) for i, s in enumerate(sh)]
        await update.effective_chat.send_message(fmt_list("الأكثر مشاهدة هذا الأسبوع", lines), parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu())
    except Exception:
        await update.effective_chat.send_message("تعذّر جلب القائمة حالياً.")

async def on_list_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    try:
        if data == "L:trend_movies":
            items = await trending_movies()
            lines = [_fmt_line_movie(m, i+1) for i, m in enumerate(items)]
            await q.edit_message_text(fmt_list("أفلام رائجة الآن", lines), parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu())
        elif data == "L:trend_shows":
            items = await trending_shows()
            lines = [_fmt_line_show(s, i+1) for i, s in enumerate(items)]
            await q.edit_message_text(fmt_list("مسلسلات رائجة الآن", lines), parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu())
        elif data == "L:week_movies":
            items = await watched_weekly_movies()
            lines = [_fmt_line_movie(m, i+1) for i, m in enumerate(items)]
            await q.edit_message_text(fmt_list("الأكثر مشاهدة هذا الأسبوع (أفلام)", lines), parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu())
        elif data == "L:week_shows":
            items = await watched_weekly_shows()
            lines = [_fmt_line_show(s, i+1) for i, s in enumerate(items)]
            await q.edit_message_text(fmt_list("الأكثر مشاهدة هذا الأسبوع (مسلسلات)", lines), parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu())
    except httpx.HTTPStatusError as e:
        await q.edit_message_text("تعذّر جلب القائمة من المزود الآن.", reply_markup=main_menu())
    except Exception:
        await q.edit_message_text("حدث خطأ غير متوقع.", reply_markup=main_menu())

# ===== App =====
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "ابدأ"),
        BotCommand("list", "عرض القوائم"),
        BotCommand("movies", "أفلام رائجة"),
        BotCommand("shows", "مسلسلات رائجة"),
        BotCommand("topweek", "Top الأسبوع"),
        BotCommand("ping", "اختبار"),
    ])

def build() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("movies", cmd_movies))
    app.add_handler(CommandHandler("shows", cmd_shows))
    app.add_handler(CommandHandler("topweek", cmd_topweek))
    app.add_handler(CallbackQueryHandler(on_list_buttons, pattern=r"^L:"))
    # أي نص عادي → افتح قائمة القوائم بدلًا من الإهمال
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_list))
    return app

if __name__ == "__main__":
    log.info("[bot] starting (lists via Trakt)…")
    build().run_polling(drop_pending_updates=True)
