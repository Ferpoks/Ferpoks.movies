# -*- coding: utf-8 -*-
import os, logging, httpx
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, Application, CommandHandler, MessageHandler, ContextTypes, filters

# ========= الإعداد =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("simplebot")

BOT_TOKEN     = (os.getenv("BOT_TOKEN") or "").strip()
OMDB_API_KEY  = (os.getenv("OMDB_API_KEY") or "").strip()
OMDB_URL      = "https://www.omdbapi.com/"

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN مفقود")
if not OMDB_API_KEY:
    raise SystemExit("OMDB_API_KEY مفقود")

client = httpx.AsyncClient(timeout=20)

# ========= أوامر =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = "👋 أهلاً! اكتب اسم فيلم/مسلسل أو استخدم: /search الاسم\nالأوامر: /ping /start"
    await update.effective_chat.send_message(txt)

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message("pong ✅")

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # اجلب الاستعلام من /search أو من النص العادي
    if context.args:
        q = " ".join(context.args)
    else:
        msg = update.message.text if update.message else ""
        q = msg.replace("/search", "", 1).strip()

    if not q:
        await update.effective_chat.send_message("اكتب: /search اسم الفيلم أو أرسل الاسم مباشرةً.")
        return

    # طلب البحث
    try:
        r = await client.get(OMDB_URL, params={"apikey": OMDB_API_KEY, "s": q, "r": "json"})
        r.raise_for_status()
        js = r.json() or {}
        results = js.get("Search") or []
    except Exception as e:
        log.exception("OMDb search failed")
        await update.effective_chat.send_message("تعذّر الوصول إلى مصدر البحث حالياً.")
        return

    if not results:
        await update.effective_chat.send_message("لم نجد نتائج.")
        return

    # أرسل أول 5 نتائج بتنسيق بسيط
    for item in results[:5]:
        title = item.get("Title", "—")
        year  = item.get("Year", "—")
        imdb  = item.get("imdbID", "")
        typ   = item.get("Type", "—")
        poster = item.get("Poster")
        cap = f"*{title}* ({year})\nالنوع: `{typ}`\nIMDb: https://www.imdb.com/title/{imdb}/"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("فتح IMDb", url=f"https://www.imdb.com/title/{imdb}/")]]) if imdb else None

        # تفاصيل أدق للصورة (اختياري)
        if poster and poster != "N/A":
            await update.effective_chat.send_photo(poster, caption=cap, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            await update.effective_chat.send_message(cap, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

# ========= تطبيق التيليجرام =========
def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("search", search))
    # أي نص ليس أمرًا = اعتبره بحثًا
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search))
    return app

if __name__ == "__main__":
    log.info("[bot] starting (OMDb only)…")
    build_app().run_polling(drop_pending_updates=True)
