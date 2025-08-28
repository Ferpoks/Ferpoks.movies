# -*- coding: utf-8 -*-
"""
بوت تيليجرام لتنزيل *قانوني* من الروابط المباشرة والمصادر المرخّصة فقط.
- يمنع المنصّات المحمية (YouTube/TikTok/Instagram/Snap/…)
- ينزّل أي ملف وسائط من رابط مباشر مسموح (video/* أو audio/*)
- يرسل الملف للمستخدم كـ Document بأعلى جودة متاحة كما هو (لا نضيف علامة مائية)
- يفحص الحجم المعلن (Content-Length) ويتوقف بأدب لو أكبر من الحد الآمن

الأوامر:
  /start      → شرح سريع
  /download <URL>  → تنزيل من رابط مباشر مسموح
  /status     → فحص بسيط لحالة البوت

بيئة التشغيل (Render → Environment):
  BOT_TOKEN=توكن_تيليجرام (مطلوب)
  MAX_BYTES=1900000000         (اختياري: الحد الأقصى ~1.9GB)

Render (Background Worker):
  Build: pip install -r requirements.txt
  Start: python3 bot_legal_downloader.py

requirements.txt المقترح:
  python-telegram-bot==21.6
  httpx==0.27.0
"""
from __future__ import annotations
import os, re, tempfile, mimetypes, logging, asyncio, pathlib
from urllib.parse import urlparse
from typing import Optional

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# ===== إعداد =====
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("legaldl")

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN مفقود")

MAX_BYTES = int(os.getenv("MAX_BYTES", "1900000000"))  # ~1.9GB افتراضيًا

# قوائم منع واضحة للمنصّات المحمية (سلاسل جزئية في الـ hostname)
DENY_HOST_SUBSTR = [
    "tiktok", "ttw", "byte",  # TikTok/CDNs
    "youtube", "youtu.be", "googlevideo", "ytimg",  # YouTube
    "instagram", "cdninstagram", "fbcdn", "facebook",  # IG/FB
    "snap", "sc-cdn",  # Snapchat
    "x.com", "twitter", "twimg",  # X/Twitter
    "spotify", "apple.com", "netflix", "disney", "primevideo", "hulu", "osn", "shahid",
]

ALLOWED_SCHEMES = {"http", "https"}

# ===== HTTP عميل =====
_http: Optional[httpx.AsyncClient] = None

def http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=60)
    return _http

# ===== أدوات =====

def is_denied_host(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        host = host.lower()
        return any(s in host for s in DENY_HOST_SUBSTR)
    except Exception:
        return True

async def head_for_meta(url: str):
    r = await http().head(url, follow_redirects=True)
    # بعض السيرفرات لا تدعم HEAD جيدًا؛ نجرب GET بدون تحميل الجسم
    if r.status_code >= 400:
        r = await http().get(url, follow_redirects=True, headers={"Range": "bytes=0-0"})
    cl = int(r.headers.get("Content-Length") or r.headers.get("content-length") or 0)
    ct = r.headers.get("Content-Type") or r.headers.get("content-type") or ""
    disp = r.headers.get("Content-Disposition") or r.headers.get("content-disposition") or ""
    return cl, ct, disp

def guess_filename(url: str, content_disposition: str) -> str:
    # من Content-Disposition
    m = re.search(r'filename\*=UTF-8''([^;\n]+)', content_disposition)
    if m:
        return m.group(1)
    m = re.search(r'filename="?([^";]+)"?', content_disposition)
    if m:
        return m.group(1)
    # من مسار الرابط
    name = pathlib.Path(urlparse(url).path).name
    if name:
        return name
    return "file"

# ===== Handlers =====
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "👋 أهلاً!\n"
        "أرسل أمر: `/download <الرابط>` لتنزيل ملف وسائط *مسموح* (من رابط مباشر فقط).\n"
        "هذا البوت لا ينزل من يوتيوب/تيك توك/انستقرام/سناب وغيرها.")
    await update.effective_chat.send_message(txt, parse_mode=ParseMode.MARKDOWN)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok = "✅"; no = "❌"
    await update.effective_chat.send_message(
        f"• BOT_TOKEN: {ok}\n"
        f"• MAX_BYTES: `{MAX_BYTES}`\n"
        f"• القيود: منع المنصّات المحمية مفعّل",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # اجلب الرابط
    args = context.args or []
    if not args:
        await update.effective_chat.send_message("استخدم: `/download https://example.com/video.mp4`", parse_mode=ParseMode.MARKDOWN)
        return
    url = args[0].strip()
    # تحقق من الصيغة
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES or not parsed.netloc:
        await update.effective_chat.send_message("رابط غير صالح.")
        return
    # منع المنصّات المحمية
    if is_denied_host(url):
        await update.effective_chat.send_message("هذا الرابط من منصة محمية—لا يمكن تنزيله هنا. أرسل رابطًا مباشرًا مسموحًا.")
        return

    # قراءة الميتاداتا
    try:
        size, ctype, disp = await head_for_meta(url)
    except Exception:
        await update.effective_chat.send_message("تعذّر الوصول للرابط.")
        return

    # القبول: محتوى فيديو/صوت أو امتداد معروف
    ext = pathlib.Path(urlparse(url).path).suffix.lower()
    ok_mime = (ctype.startswith("video/") or ctype.startswith("audio/"))
    ok_ext = ext in {".mp4", ".mov", ".mkv", ".webm", ".m4a", ".mp3", ".aac"}
    if not (ok_mime or ok_ext):
        await update.effective_chat.send_message("الرابط لا يبدو كملف وسائط مباشر.")
        return

    # حجم آمن
    if size and size > MAX_BYTES:
        await update.effective_chat.send_message("الملف كبير جدًا بالنسبة للإرسال هنا.")
        return

    file_name = guess_filename(url, disp)
    if not pathlib.Path(file_name).suffix:
        # حاول تخمين الامتداد من المايم تايب
        ext2 = mimetypes.guess_extension(ctype) or ext or ".bin"
        file_name += ext2

    await update.effective_chat.send_message("جاري التنزيل… قد يستغرق بعض الوقت حسب الحجم.")

    # تنزيل إلى ملف مؤقت ثم الإرسال
    try:
        async with http().stream("GET", url, follow_redirects=True) as r:
            r.raise_for_status()
            with tempfile.NamedTemporaryFile(prefix="dl_", delete=False) as f:
                total = 0
                async for chunk in r.aiter_bytes(64 * 1024):
                    f.write(chunk)
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise RuntimeError("file too large")
                temp_path = f.name
        # أرسل
        await update.effective_chat.send_document(document=open(temp_path, "rb"), filename=file_name, caption=f"تم التحميل ✅\n{file_name}")
    except RuntimeError:
        await update.effective_chat.send_message("الحجم تعدّى الحد المسموح.")
    except httpx.HTTPError:
        await update.effective_chat.send_message("فشل التحميل من المصدر.")
    except Exception:
        await update.effective_chat.send_message("حدث خطأ غير متوقع أثناء التحميل.")
    finally:
        # نظّف الملف المؤقت
        try:
            if 'temp_path' in locals():
                os.remove(temp_path)
        except Exception:
            pass

# أي نص غير الأوامر → مساعدة قصيرة
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)

# ===== تطبيق =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("download", cmd_download))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(drop_pending_updates=True)
