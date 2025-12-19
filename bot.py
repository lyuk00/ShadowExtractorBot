import os
import tempfile
import requests
import re
from threading import Thread
from flask import Flask
from telegram import Update, InputMediaPhoto
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp

TOKEN = os.getenv("TOKEN")

# Flask app finta per tenere Render sveglio
app = Flask(__name__)

@app.route('/')
def home():
    return "Shadow Extractor System is alive. Ready to raid gates. 🗡️", 200

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))

# Regex per URL
URL_REGEX = re.compile(r'https?://[^\s]+', re.IGNORECASE)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🗡️ Shadow Extractor System activated.\n\n"
        "I am the Gatekeeper of forbidden content.\n"
        "Send me a link from:\n"
        "• Instagram Reels\n"
        "• TikTok (videos & photo gates)\n"
        "• YouTube Shorts\n"
        "• And many other dungeons...\n\n"
        "I will extract the essence without watermark. ⚔️\n"
        "Level up your library. Rise, Hunter."
    )

async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = update.message.text or ""
    
    urls = URL_REGEX.findall(message_text)
    
    if not urls:
        return
    
    url = urls[0]
    
    status_msg = await update.message.reply_text("🗡️ Opening the Gate... Extracting shadow essence.")

    if "tiktok" in url.lower():
        await status_msg.edit_text("🗡️ TikTok Gate detected... entering Shadow Realm.")
    else:
        ydl_opts = {
            'format': 'best[height<=720]/best',  # Fix YouTube: evita formati che richiedono login
            'outtmpl': '%(title)s.%(ext)s',
            'noplaylist': True,
            'merge_output_format': 'mp4',
            'quiet': True,
            'no_warnings': True,
            'cookiefile': '',  # Evita richieste login dove possibile
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts['outtmpl'] = os.path.join(tmpdir, '%(title)s.%(ext)s')
            
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    filename = ydl.prepare_filename(info)
                
                await status_msg.edit_text("⚔️ Extraction complete. Delivering the loot...")
                
                with open(filename, 'rb') as video_file:
                    await update.message.reply_video(
                        video=video_file,
                        caption=f"🗡️ {info.get('title', 'Essence')}\nExtracted from {info.get('extractor_key', 'Gate')}\nRank up, Hunter."
                    )
                
                await status_msg.delete()
                return
            
            except Exception as e:
                await status_msg.edit_text(f"❌ Gate collapsed: unable to breach this dungeon.\nError: {str(e)[:150]}")
                return

    # Fallback TikTok
    try:
        api_url = "https://www.tikwm.com/api/"
        params = {"url": url}
        response = requests.get(api_url, params=params, timeout=30)
        data = response.json()
        
        if data.get("code") != 0:
            raise Exception("Shadow Realm sealed")
        
        video_data = data["data"]
        title = video_data.get("title", "Shadow Essence").strip()
        music_title = video_data.get("music_info", {}).get("title", "Necromancer's Tune")
        music_url = video_data["music"]
        
        if video_data.get("images"):
            await status_msg.edit_text(f"🗡️ Photo Gate breached!\n{len(video_data['images'])} shadows + BGM extracted.")
            media_group = [InputMediaPhoto(media=requests.get(img, timeout=30).content) for img in video_data["images"]]
            await update.message.reply_media_group(media=media_group)
            
            music_resp = requests.get(music_url, timeout=60)
            await update.message.reply_audio(audio=music_resp.content, caption=f"🎵 BGM: {music_title}")
            await status_msg.delete()
            return
        
        video_url = video_data.get("play") or video_data.get("hdplay") or video_data.get("wmplay")
        video_resp = requests.get(video_url, timeout=60)
        await update.message.reply_video(video=video_resp.content, caption=f"🗡️ {title}\nCleared without watermark ⚔️")
        
        music_resp = requests.get(music_url, timeout=60)
        await update.message.reply_audio(audio=music_resp.content, caption=f"🎵 Original BGM: {music_title}")
        
        await status_msg.delete()
        return
    
    except Exception as err:
        await status_msg.edit_text(f"❌ Gate collapsed: {str(err)[:200]}")

def main():
    # Avvia Flask in background
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Avvia il bot Telegram
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_video))
    
    print("Shadow Extractor System online... Ready to raid gates. 🗡️")
    app.run_polling()

if __name__ == '__main__':
    main()
