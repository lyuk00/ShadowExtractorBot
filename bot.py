import os
import tempfile
import re
import asyncio
import aiohttp
from threading import Thread
from flask import Flask
from telegram import Update, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp

# ===============================
# ENV
# ===============================
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN not found in environment variables.")

# ===============================
# Flask app (Render keep-alive)
# ===============================
app = Flask(__name__)

@app.route("/")
def home():
    return "Zani is online and ready.", 200

# ===============================
# Regex URL
# ===============================
URL_REGEX = re.compile(r'https?://[^\s]+', re.IGNORECASE)

# ===============================
# Telegram handlers
# ===============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    testo_benvenuto = (
        "Good day.\n\n"
        "Send me a link from:\n"
        "• YouTube (including Shorts)\n"
        "• TikTok\n"
        "• Instagram\n"
        "• X (Twitter)\n\n"
        "I will handle the rest and return it in the best quality available."
    )
    await update.message.reply_text(testo_benvenuto)

# Funzione yt-dlp in thread
def esegui_ytdlp(url, tmpdir):
    ydl_opts = {
        'format': 'bestvideo[filesize<45M]+bestaudio/best[filesize<45M]/best',
        'noplaylist': True,
        'merge_output_format': 'mp4',
        'quiet': True,
        'no_warnings': True,
        'outtmpl': os.path.join(tmpdir, '%(id)s.%(ext)s'),
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filepath = ydl.prepare_filename(info)
        return info, filepath

async def download_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    message_text = update.message.text or update.message.caption or ""
    urls = URL_REGEX.findall(message_text)

    if not urls:
        return

    url = urls[0].lower()

    # Solo link supportati
    supported_domains = [
        "youtube.com",
        "youtu.be",
        "tiktok.com",
        "vm.tiktok.com",
        "instagram.com",
        "x.com",
        "twitter.com"
    ]

    if not any(domain in url for domain in supported_domains):
        return   # Ignora link non supportati

    try:
        await update.message.delete()
    except Exception:
        pass

    status_msg = await update.message.reply_text("⏳ Give me a moment. I'm checking the link...")

    try:
        # ================= TikTok =================
        if "tiktok.com" in url or "vm.tiktok.com" in url:
            async with aiohttp.ClientSession() as session:
                api_url = "https://www.tikwm.com/api/"
                async with session.get(api_url, params={"url": url}) as resp:
                    data = await resp.json()

            if data.get("code") != 0:
                raise Exception("Unable to retrieve the media data.")

            video_data = data["data"]
            title = video_data.get("title", "TikTok Media").strip()

            if video_data.get("images"):
                # Carousel di immagini
                media_group = [InputMediaPhoto(media=img) for img in video_data["images"][:10]]
                await update.message.reply_media_group(media=media_group)
                await status_msg.delete()
                return

            video_url = video_data.get("play")
            caption = f"📱 <b>{title[:100]}</b>\n\n🔗 <a href='{url}'>Original Source</a>"

            await update.message.reply_video(
                video=video_url,
                caption=caption,
                parse_mode=ParseMode.HTML
            )
            await status_msg.delete()
            return

        # ================= YouTube / Instagram / X =================
        await status_msg.edit_text("⬇️ Retrieving the media...")

        with tempfile.TemporaryDirectory() as tmpdir:
            info, filepath = await asyncio.to_thread(esegui_ytdlp, url, tmpdir)

            title = info.get("title", "Media Content")
            caption = f"🎬 <b>{title[:100]}</b>\n\n🔗 <a href='{url}'>Original Source</a>"

            if not os.path.exists(filepath):
                thumbnail = info.get("thumbnail")
                if thumbnail:
                    await update.message.reply_photo(
                        photo=thumbnail,
                        caption=caption,
                        parse_mode=ParseMode.HTML
                    )
                else:
                    await status_msg.edit_text("❌ No supported media was found in this link.")
                await status_msg.delete()
                return

            await status_msg.edit_text("🚀 Everything is ready. Sending it now...")

            with open(filepath, "rb") as video_file:
                await update.message.reply_video(
                    video=video_file,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True
                )

            await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(
            f"❌ The process was interrupted.\n<code>{str(e)[:150]}</code>",
            parse_mode=ParseMode.HTML
        )

# ===============================
# Startup
# ===============================
def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()

    tg_app = Application.builder().token(TOKEN).build()

    tg_app.add_handler(CommandHandler("start", start))

    tg_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, download_media)
    )

    print("Zani is online and ready.")
    tg_app.run_polling()
