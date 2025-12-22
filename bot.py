#!/usr/bin/env python3
import os
import re
import sqlite3
import tempfile
import logging
import subprocess
import asyncio
from threading import Thread
from typing import Optional, Tuple, List, Dict
from datetime import datetime

import requests
import yt_dlp
from flask import Flask
from telegram import Update, InputMediaPhoto, InputMediaVideo
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ===============================
# CONFIG
# ===============================
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN not found in environment variables")

TELEGRAM_LIMIT_BYTES = int(os.getenv("TELEGRAM_LIMIT_BYTES", 48 * 1024 * 1024))  # default 48MB
MAX_DOWNLOAD_RETRIES = int(os.getenv("MAX_DOWNLOAD_RETRIES", 2))
DB_PATH = os.getenv("CACHE_DB", "cache.db")
COOKIEFILE = os.getenv("COOKIEFILE")  # optional path to cookies.txt
CAPTION_TEMPLATE_HTML = os.getenv(
    "CAPTION_TEMPLATE_HTML",
    "<b>🗡️ Shadow Extractor</b>\n"
    "<i>gate</i>: <a href=\"{url}\">source</a>\n"
    "⏱️ <b>Durata</b>: {duration}s   🎚️ <b>Qualità</b>: {height}p\n"
    "<code>lvl={duration}s</code>"
)

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
# LOGGING
# ===============================
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("shadow_extractor")

# ===============================
# FLASK KEEPALIVE
# ===============================
app = Flask(__name__)

@app.route("/")
def home():
    return "Shadow Extractor System online 🗡️", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ===============================
# SQLITE CACHE
# ===============================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS cache (
        url TEXT PRIMARY KEY,
        file_id TEXT,
        kind TEXT,
        size INTEGER,
        duration INTEGER,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()

def get_cache(url: str) -> Optional[Tuple[str, str, int, int]]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT file_id, kind, size, duration FROM cache WHERE url = ?", (url,))
    row = cur.fetchone()
    conn.close()
    return row if row else None

def set_cache(url: str, file_id: str, kind: str, size: int, duration: Optional[int]):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO cache(url, file_id, kind, size, duration)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            file_id=excluded.file_id,
            kind=excluded.kind,
            size=excluded.size,
            duration=excluded.duration,
            updated_at=CURRENT_TIMESTAMP
    """, (url, file_id, kind, size, duration or 0))
    conn.commit()
    conn.close()

# ===============================
# HELPERS
# ===============================
def build_caption_html(url: str, info: Optional[dict] = None) -> str:
    duration = "unknown"
    height = "unknown"
    try:
        if info and info.get("duration"):
            duration = int(info.get("duration"))
    except Exception:
        duration = "unknown"
    try:
        formats = info.get("formats") or []
        if formats:
            best = max(formats, key=lambda f: f.get("height") or 0)
            height = best.get("height") or "unknown"
    except Exception:
        height = "unknown"
    return CAPTION_TEMPLATE_HTML.format(url=url, duration=duration, height=height)

async def run_subprocess(cmd: List[str]):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE))

def transcode_high_quality(input_path: str, output_path: str, max_bytes: int, max_width=1920, max_height=1080) -> str:
    crf = 18
    while True:
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-c:v", "libx264",
            "-preset", "slow",
            "-crf", str(crf),
            "-maxrate", "8M",
            "-bufsize", "16M",
            "-vf", f"scale='min({max_width},iw)':'min({max_height},ih)':force_original_aspect_ratio=decrease",
            "-c:a", "aac",
            "-b:a", "128k",
            output_path
        ]
        logger.info("Running ffmpeg CRF=%s", crf)
        subprocess.run(cmd, check=True)
        size = os.path.getsize(output_path)
        logger.info("Transcoded size: %d bytes (limit %d)", size, max_bytes)
        if size <= max_bytes or crf >= 28:
            return output_path
        crf += 2

def has_video_in_formats(obj: Dict) -> bool:
    formats = obj.get("formats") or []
    for f in formats:
        vcodec = (f.get("vcodec") or "").lower()
        if vcodec and vcodec != "none":
            return True
    req = obj.get("requested_formats") or []
    for f in req:
        if (f.get("vcodec") or "").lower() != "none":
            return True
    if obj.get("duration"):
        return True
    return False

def extract_image_urls(info_obj: dict) -> List[str]:
    images = []
    # if object contains video, do not return images
    if has_video_in_formats(info_obj):
        return []
    entries = info_obj.get("entries") or []
    for e in entries:
        if has_video_in_formats(e):
            continue
        ext = (e.get("ext") or "").lower()
        if e.get("url") and ext in ("jpg", "jpeg", "png", "webp"):
            images.append(e["url"])
    if not images and info_obj.get("url") and (info_obj.get("ext") or "").lower() in ("jpg", "jpeg", "png", "webp"):
        images.append(info_obj["url"])
    if not images and info_obj.get("thumbnails"):
        for t in info_obj["thumbnails"]:
            if t.get("url"):
                images.append(t["url"])
    return images

async def send_image_group_safe(message, image_urls: List[str], caption: Optional[str]):
    chunk_size = 10
    for i in range(0, len(image_urls), chunk_size):
        chunk = image_urls[i:i+chunk_size]
        media = []
        for j, img in enumerate(chunk):
            media.append(InputMediaPhoto(media=img, caption=caption if i == 0 and j == 0 else None))
        try:
            await message.reply_media_group(media)
        except Exception as e:
            logger.exception("media_group failed, falling back to single sends: %s", e)
            for j, img in enumerate(chunk):
                try:
                    await message.reply_photo(photo=img, caption=caption if i == 0 and j == 0 else None)
                except Exception as e2:
                    logger.exception("single photo send failed for %s: %s", img, e2)

# ===============================
# TELEGRAM HANDLERS
# ===============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🗡️ Shadow Extractor ready. Invia un link supportato.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Invia un link (TikTok, Instagram, X, YouTube). Il bot prova a inviare il file in massima qualità.\n"
        "Se il file è troppo grande, viene ricodificato mantenendo alta qualità.\n"
        "Comandi: /start /help /ping /stats"
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ gate responsiveness: OK")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT COUNT(*) FROM cache")
    total = cur.fetchone()[0]
    conn.close()
    await update.message.reply_text(f"Cache entries: {total}")

async def download_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    text = message.text.strip()
    urls = URL_REGEX.findall(text)
    if not urls:
        return

    url = urls[0]
    if not any(domain in url.lower() for domain in ALLOWED_DOMAINS):
        return

    logger.info("Request for URL: %s from %s", url, message.from_user.id)
    status = await message.reply_text("🗡️ Opening the gate...")

    # cache check
    cached = get_cache(url)
    if cached:
        file_id, kind, size, duration = cached
        logger.info("Cache hit for %s kind=%s size=%s", url, kind, size)
        caption = build_caption_html(url, {"duration": duration})
        try:
            if kind == "video":
                await message.reply_video(video=file_id, caption=caption, parse_mode="HTML", disable_web_page_preview=True)
            elif kind == "photo":
                await message.reply_photo(photo=file_id, caption=caption, parse_mode="HTML", disable_web_page_preview=True)
            else:
                await message.reply_document(document=file_id, caption=caption, parse_mode="HTML", disable_web_page_preview=True)
            await status.delete()
            return
        except Exception as e:
            logger.exception("Failed to send cached file_id, will attempt fresh download: %s", e)

    # TikTok quick API
    if "tiktok.com" in url.lower():
        try:
            api_url = "https://www.tikwm.com/api/"
            r = requests.get(api_url, params={"url": url}, timeout=30)
            r.raise_for_status()
            data = r.json()
            if data.get("code") == 0:
                data = data["data"]
                if data.get("images"):
                    images = data["images"]
                    await send_image_group_safe(message, images, build_caption_html(url, None))
                    await status.delete()
                    return
                video_url = data.get("hdplay") or data.get("play")
                if video_url:
                    sent = await message.reply_video(video=video_url, caption=build_caption_html(url, None), parse_mode="HTML", disable_web_page_preview=True)
                    try:
                        if sent.video:
                            set_cache(url, sent.video.file_id, "video", sent.video.file_size or 0, sent.video.duration or 0)
                    except Exception:
                        pass
                    await status.delete()
                    return
        except Exception as e:
            logger.exception("TikTok API fallback failed: %s", e)

    # yt-dlp probe
    ydl_opts_probe = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": False,
    }
    if COOKIEFILE:
        ydl_opts_probe["cookiefile"] = COOKIEFILE

    info = None
    for attempt in range(MAX_DOWNLOAD_RETRIES + 1):
        try:
            with yt_dlp.YoutubeDL(ydl_opts_probe) as ydl:
                info = ydl.extract_info(url, download=False)
            break
        except Exception as e:
            logger.exception("yt-dlp probe attempt %d failed for %s: %s", attempt + 1, url, e)
            if attempt < MAX_DOWNLOAD_RETRIES:
                await asyncio.sleep(1 + attempt * 2)
            else:
                await status.edit_text("❌ Gate collapsed while scanning the realm (metadata).")
                return

    if not info:
        await status.edit_text("❌ No metadata found for this gate.")
        return

    extractor = (info.get("extractor_key") or "").lower()
    logger.info("Extractor: %s", extractor)

    # decide video vs images
    if has_video_in_formats(info):
        # proceed to video path
        pass
    else:
        image_urls = extract_image_urls(info)
        if image_urls:
            caption = build_caption_html(url, info)
            try:
                await send_image_group_safe(message, image_urls, caption)
                await status.delete()
                return
            except Exception as e:
                logger.exception("Failed to send images, will try video path if available: %s", e)
                # continue to video path as fallback

    # VIDEO PATH
    ydl_opts_dl = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(tempfile.gettempdir(), "shadow_%(id)s.%(ext)s"),
    }
    if COOKIEFILE:
        ydl_opts_dl["cookiefile"] = COOKIEFILE

    filename = None
    final_info = None
    for attempt in range(MAX_DOWNLOAD_RETRIES + 1):
        try:
            with yt_dlp.YoutubeDL(ydl_opts_dl) as ydl:
                final_info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(final_info)
            break
        except Exception as e:
            logger.exception("yt-dlp download attempt %d failed for %s: %s", attempt + 1, url, e)
            if attempt < MAX_DOWNLOAD_RETRIES:
                await asyncio.sleep(1 + attempt * 2)
            else:
                await status.edit_text("❌ Gate collapse while materializing the file.")
                return

    if not filename or not os.path.exists(filename):
        logger.error("Downloaded file not found for %s", url)
        await status.edit_text("❌ Artifact not produced by the gate.")
        return

    size = os.path.getsize(filename)
    duration = final_info.get("duration") if final_info else None
    caption = build_caption_html(url, final_info)

    logger.info("Downloaded file %s size=%d duration=%s", filename, size, duration)

    # send original if within limit
    if size <= TELEGRAM_LIMIT_BYTES:
        try:
            with open(filename, "rb") as f:
                sent = await message.reply_video(video=f, caption=caption, parse_mode="HTML", disable_web_page_preview=True)
            try:
                if sent.video:
                    set_cache(url, sent.video.file_id, "video", sent.video.file_size or size, sent.video.duration or duration)
            except Exception:
                pass
            await status.delete()
            try:
                os.remove(filename)
            except Exception:
                pass
            return
        except Exception as e:
            logger.exception("Failed to send original video: %s", e)

    # transcode if too large
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, f"transcoded_{os.path.basename(filename)}")
            logger.info("Transcoding %s -> %s", filename, out_path)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, transcode_high_quality, filename, out_path, TELEGRAM_LIMIT_BYTES, 1920, 1080)
            if not os.path.exists(out_path):
                logger.error("Transcode produced no file")
                await status.edit_text("❌ Gate failed during transcode.")
                return
            out_size = os.path.getsize(out_path)
            logger.info("Transcoded file size: %d", out_size)
            if out_size > TELEGRAM_LIMIT_BYTES:
                await status.edit_text("❌ Artifact too heavy even after transcode.")
                return
            with open(out_path, "rb") as f:
                sent = await message.reply_video(video=f, caption=caption, parse_mode="HTML", disable_web_page_preview=True)
            try:
                if sent.video:
                    set_cache(url, sent.video.file_id, "video", sent.video.file_size or out_size, sent.video.duration or duration)
            except Exception:
                pass
            await status.delete()
            try:
                os.remove(filename)
            except Exception:
                pass
            return
    except Exception as e:
        logger.exception("Transcode/send error: %s", e)
        await status.edit_text("❌ Gate failed while optimizing the artifact.")
        try:
            await message.reply_text("Attempting to send as document (may fail if too large)...")
            with open(filename, "rb") as f:
                await message.reply_document(document=f, caption=caption, parse_mode="HTML", disable_web_page_preview=True)
            await status.delete()
            return
        except Exception as e2:
            logger.exception("Final document send failed: %s", e2)
            await status.edit_text("❌ Final gate attempt failed.")
            return

# ===============================
# STARTUP
# ===============================
def main():
    init_db()
    Thread(target=run_flask, daemon=True).start()

    app_bot = Application.builder().token(TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CommandHandler("help", help_cmd))
    app_bot.add_handler(CommandHandler("ping", ping))
    app_bot.add_handler(CommandHandler("stats", stats_cmd))
    app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_media))

    logger.info("Starting Telegram polling...")
    app_bot.run_polling()

if __name__ == "__main__":
    main()
