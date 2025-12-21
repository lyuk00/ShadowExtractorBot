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
# Flask app (Render keep-alive)
# ===============================
app = Flask(__name__)

@app.route("/")
def home():
    return "Shadow Extractor System is alive. Ready to raid gates. 🗡️", 200

# ===============================
# Regex URL
# ===============================
URL_REGEX = re.compile(r'https?://[^\s]+', re.IGNORECASE)

SUPPORTED_DOMAINS = (
    "tiktok.com",
    "instagram.com",
    "youtu",
    "x.com",
    "twitter.com",
)

# ===============================
# Telegram handlers
# ===============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🗡️ Shadow Extractor System activated.\n\n"
        "I am the Gatekeeper of forbidden content.\n"
        "Send me a link from:\n"
        "• Instagram\n"
        "• TikTok\n"
        "• YouTube\n"
        "• X (Twitter)\n\n"
        "I will extract the essence without watermark. ⚔️\n"
        "Rise, Hunter."
    )

async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    urls = URL_REGEX.findall(text)
    if not urls:
        return

    url = urls[0]              # URL ORIGINALE (NON TOCCARE)
    url_l = url.lower()        # solo per controlli

    if not any(d in url_l for d in SUPPORTED_DOMAINS):
        return

    status_msg = await update.message.reply_text(
        "🗡️ Opening the Gate... Extracting shadow essence."
    )

    # ===============================
    # TikTok via API
    # ===============================
    if "tiktok.com" in url_l:
        await status_msg.edit_text("🗡️ TikTok Gate detected... entering Shadow Realm.")

        try:
            api_url = "https://www.tikwm.com/api/"
            data = requests.get(api_url, params={"url": url}, timeout=30).json()

            if data.get("code") != 0:
                raise Exception("Shadow Realm sealed")

            video_data = data["data"]
            title = video_data.get("title", "Shadow Essence")

            if video_data.get("images"):
                media = [InputMediaPhoto(media=img) for img in video_data["images"]]
                for i in range(0, len(media), 10):
                    await update.message.reply_media_group(media=media[i:i+10])
                await status_msg.delete()
                return

            video_url = (
                video_data.get("hdplay")
                or video_data.get("play")
                or video_data.get("wmplay")
            )

            await update.message.reply_video(
                video=video_url,
                caption=f"🗡️ {title}\nCleared without watermark ⚔️"
            )
            await status_msg.delete()
            return

        except Exception:
            await status_msg.delete()
            return

    # ===============================
    # yt-dlp (IG / X / YT)
    # ===============================
    cookies_path = "cookies.txt" if os.path.exists("cookies.txt") else None

    is_instagram = "instagram.com" in url_l
    is_twitter   = "x.com" in url_l or "twitter.com" in url_l

    ydl_opts = {
        "merge_output_format": "mp4",
        "noplaylist": False,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
    }

    # FORMAT CORRETTO PER PIATTAFORMA
    if is_instagram:
        ydl_opts["format"] = "best"
        ydl_opts["extractor_args"] = {
            "instagram": {
                "skip_auth": True,
                "include_reels": True
            }
        }
    else:
        ydl_opts["format"] = "bestvideo+bestaudio/best"

    if cookies_path:
        ydl_opts["cookiefile"] = cookies_path

    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts["outtmpl"] = os.path.join(tmpdir, "%(title)s.%(ext)s")

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)

            # ===============================
            # FOTO / GALLERY (IG & X)
            # ===============================
            if info.get("_type") == "playlist" and info.get("entries"):
                images = []
                for entry in info["entries"]:
                    img = entry.get("url") or entry.get("thumbnail")
                    if img:
                        images.append(InputMediaPhoto(media=img))

                if images:
                    await status_msg.edit_text(
                        "🗡️ Image Gate breached... shadows unleashed."
                    )
                    for i in range(0, len(images), 10):
                        await update.message.reply_media_group(
                            media=images[i:i+10]
                        )
                    await status_msg.delete()
                    return

            # ===============================
            # VIDEO
            # ===============================
            filename = ydl.prepare_filename(info)

            if not filename or not os.path.exists(filename):
                await status_msg.delete()
                return

            if os.path.getsize(filename) > 45 * 1024 * 1024:
                await status_msg.delete()
                return

            await status_msg.edit_text("⚔️ Extraction complete. Delivering the loot...")

            with open(filename, "rb") as f:
                await update.message.reply_video(
                    video=f,
                    caption=f"🗡️ {info.get('title','Essence')}\nRank up, Hunter."
                )

            await status_msg.delete()

        except Exception:
            await status_msg.delete()
            return

# ===============================
# Run Flask + Telegram
# ===============================
def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()

    tg_app = Application.builder().token(TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, download_video)
    )

    print("Shadow Extractor System online... Ready to raid gates. 🗡️")
    tg_app.run_polling()
