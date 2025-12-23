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

# ===============================
# Gate resolver
# ===============================
def get_gate_from_url(url: str) -> str:
    u = url.lower()
    if "tiktok" in u:
        return "🟣 Purple Gate — TikTok"
    if "instagram" in u:
        return "🟠 Orange Gate — Instagram"
    if "twitter" in u or "x.com" in u:
        return "⚫ Black Gate — X"
    if "youtube" in u or "youtu.be" in u:
        return "🟥 Red Gate — YouTube"
    return ""

# ===============================
# Telegram handlers
# ===============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🗡️ Shadow Extractor System activated.\n\n"
        "Send a link from:\n"
        "• YouTube\n"
        "• TikTok\n"
        "• Instagram\n"
        "• Twitter / X\n\n"
        "I will extract the essence.\n"
        "Rise, Hunter."
    )

async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = update.message.text or ""
    urls = URL_REGEX.findall(message_text)
    if not urls:
        return

    url = urls[0]
    gate_label = get_gate_from_url(url)
    if not gate_label:
        return  # ignora link non supportati

    status_msg = await update.message.reply_text(
        "🗡️ Opening the Gate... Extracting shadow essence."
    )

    # ===============================
    # TikTok via API
    # ===============================
    if "tiktok" in url.lower():
        await status_msg.edit_text("🟣 Entering Purple Gate... TikTok dungeon detected.")
        try:
            api_url = "https://www.tikwm.com/api/"
            response = requests.get(api_url, params={"url": url}, timeout=30)
            data = response.json()

            if data.get("code") != 0:
                raise Exception("Shadow Realm sealed")

            video_data = data["data"]
            title = video_data.get("title", "Shadow Essence").strip()
            music_title = video_data.get("music_info", {}).get("title", "Unknown")

            if video_data.get("images"):
                await status_msg.edit_text(
                    f"🟣 Purple Gate cleared\n📸 {len(video_data['images'])} shadows extracted"
                )
                media_group = [
                    InputMediaPhoto(media=requests.get(img, timeout=30).content)
                    for img in video_data["images"]
                ]
                await update.message.reply_media_group(media=media_group)
                await status_msg.delete()
                return

            video_url = video_data.get("hdplay") or video_data.get("play")
            video_resp = requests.get(video_url, timeout=60)

            await update.message.reply_video(
                video=video_resp.content,
                caption=(
                    "🟣 Purple Gate — TikTok\n"
                    "⚔️ MAX QUALITY\n\n"
                    f"🗡️ {title}\n"
                    "#tiktok #shadowextractor"
                ),
            )
            await status_msg.delete()
            return

        except Exception as err:
            await status_msg.edit_text(f"❌ Gate collapsed: {str(err)[:200]}")
            return

    # ===============================
    # Everything else via yt-dlp
    # ===============================
    await status_msg.edit_text("🗡️ Attempting MAX QUALITY extraction...")

    ydl_opts_high = {
        "format": "bestvideo+bestaudio/best",
        "noplaylist": True,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "user_agent": "Mozilla/5.0",
        "cookiefile": "cookies.txt",
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts_high["outtmpl"] = os.path.join(tmpdir, "%(title)s.%(ext)s")

        try:
            with yt_dlp.YoutubeDL(ydl_opts_high) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
            quality_note = "MAX QUALITY"
        except:
            await status_msg.edit_text("⚠️ MAX QUALITY blocked — falling back.")
            ydl_opts_safe = {
                "format": "best[height<=720]/best",
                "noplaylist": True,
                "merge_output_format": "mp4",
                "quiet": True,
                "no_warnings": True,
                "retries": 3,
                "user_agent": "Mozilla/5.0",
            }
            ydl_opts_safe["outtmpl"] = os.path.join(tmpdir, "%(title)s.%(ext)s")
            with yt_dlp.YoutubeDL(ydl_opts_safe) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
            quality_note = "HIGH QUALITY (fallback)"

        title = info.get("title", "Unknown Essence")
        height = info.get("height")
        tags = info.get("tags") or []
        tags_text = " ".join(f"#{t.replace(' ', '')}" for t in tags[:5])

        caption = f"{gate_label}\n⚔️ {quality_note}"
        if height:
            caption += f" • {height}p"
        caption += (
            f"\n\n🗡️ {title}\n\n"
            f"{tags_text}\n"
            "#shadowextractor #hunter"
        )

        await status_msg.edit_text("⚔️ Extraction complete. Delivering the loot...")

        with open(filename, "rb") as video_file:
            await update.message.reply_video(
                video=video_file,
                caption=caption,
            )

        await status_msg.delete()

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
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_video))

    print("Shadow Extractor System online... Ready to raid gates. 🗡️")
    tg_app.run_polling()
