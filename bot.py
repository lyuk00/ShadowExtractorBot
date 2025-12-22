import os
import tempfile
import requests
import re
from threading import Thread
from flask import Flask
from telegram import Update, InputMediaPhoto
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp

# ===============================
# ENV
# ===============================
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN not found in environment variables")

# ===============================
# Flask (Render keep-alive)
# ===============================
app = Flask(__name__)

@app.route("/")
def home():
    return "Shadow Extractor System online 🗡️", 200

# ===============================
# URL regex + allowed domains
# ===============================
URL_REGEX = re.compile(r'https?://[^\s]+', re.IGNORECASE)

ALLOWED_DOMAINS = (
    "tiktok.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "youtu.be",
    "youtube.com",
)

# ===============================
# Telegram handlers
# ===============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🗡️ Shadow Extractor ready.")

# ===============================
# MAIN HANDLER
# ===============================
async def download_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    urls = URL_REGEX.findall(text)
    if not urls:
        return

    url = urls[0]

    # 🔒 IGNORA link non supportati
    if not any(domain in url.lower() for domain in ALLOWED_DOMAINS):
        return

    status = await update.message.reply_text("🗡️ Opening the gate...")

    # ===============================
    # TikTok (API)
    # ===============================
    if "tiktok.com" in url.lower():
        try:
            api_url = "https://www.tikwm.com/api/"
            r = requests.get(api_url, params={"url": url}, timeout=30).json()
            if r.get("code") != 0:
                raise Exception("TikTok gate sealed")

            data = r["data"]

            # Photo post
            if data.get("images"):
                media = [InputMediaPhoto(media=img) for img in data["images"]]
                await update.message.reply_media_group(media)
                await status.delete()
                return

            # Video
            video_url = data.get("hdplay") or data.get("play")
            await update.message.reply_video(video=video_url)
            await status.delete()
            return

        except:
            await status.delete()
            return

    # ===============================
    # yt-dlp (X / IG / YT)
    # ===============================
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "cookiefile": "cookies.txt",
        "extract_flat": False,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except:
        await status.delete()
        return

    # ===============================
    # X / Twitter PHOTO TWEET
    # ===============================
    if "twitter" in info.get("extractor_key", "").lower():
        entries = info.get("entries") or []

        images = []
        for e in entries:
            if e.get("url") and e.get("ext") in ("jpg", "png", "webp"):
                images.append(e["url"])

        if images:
            media = [InputMediaPhoto(media=img) for img in images]
            await update.message.reply_media_group(media)
            await status.delete()
            return

    # ===============================
    # VIDEO (generic)
    # ===============================
    with tempfile.TemporaryDirectory() as tmp:
        ydl_opts_dl = {
            **ydl_opts,
            "format": "bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "outtmpl": os.path.join(tmp, "%(title)s.%(ext)s"),
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts_dl) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
        except:
            await status.delete()
            return

        with open(filename, "rb") as f:
            await update.message.reply_video(video=f)

        await status.delete()

# ===============================
# RUN
# ===============================
def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()

    tg = Application.builder().token(TOKEN).build()
    tg.add_handler(CommandHandler("start", start))
    tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_media))

    tg.run_polling()
