#!/usr/bin/env python3
import os
import re
import sqlite3
import tempfile
import logging
import subprocess
import asyncio
from threading import Thread
from typing import Optional, Tuple, List
from datetime import datetime

import requests
import yt_dlp
from flask import Flask
from telegram import Update, InputMediaPhoto
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ===============================
# CONFIG
# ===============================
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN not found in environment variables")

# Telegram limits and thresholds
TELEGRAM_LIMIT_BYTES = int(os.getenv("TELEGRAM_LIMIT_BYTES", 48 * 1024 * 1024))  # default 48MB
MAX_DOWNLOAD_RETRIES = 2
DB_PATH = os.getenv("CACHE_DB", "cache.db")
COOKIEFILE = os.getenv("COOKIEFILE")  # optional path to cookies.txt if you have one
CAPTION_TEMPLATE = os.getenv("CAPTION_TEMPLATE", "🗡️ gate={url}\nlvl={duration}s\nsource=shadow-extractor")

# Allowed domains
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
# Flask keep-alive (optional)
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
def build_caption(url: str, info: Optional[dict] = None) -> str:
    duration = None
    if info:
        duration = info.get("duration")
        try:
            duration = int(duration) if duration else None
        except Exception:
            duration = None
    return CAPTION_TEMPLATE.format(url=url, duration=duration or "unknown")

async def run_subprocess(cmd: List[str]):
    """Run blocking subprocess in thread-safe way."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE))

def transcode_high_quality(input_path: str, output_path: str, max_bytes: int, max_width=1920, max_height=1080) -> str:
    """
    Blocking function: transcode with ffmpeg preserving high quality.
    Uses CRF loop to reduce size if needed.
    """
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
        logger.info("Running ffmpeg: CRF=%s", crf)
        subprocess.run(cmd, check=True)
        size = os.path.getsize(output_path)
        logger.info("Transcoded size: %d bytes (limit %d)", size, max_bytes)
        if size <= max_bytes or crf >= 28:
            return output_path
        crf += 2  # increase CRF to reduce size

def choose_best_format(info: dict) -> dict:
    """
    Return info dict for the best available format (prefer highest quality).
    If filesize is present and too large, still return best; decision to transcode later.
    """
    # If formats available, pick the one with highest resolution/bitrate
    formats = info.get("formats") or []
    if not formats:
        return info
    # prefer formats with video+audio merged or bestvideo+bestaudio selection handled by yt-dlp
    # choose format with largest height then filesize
    def score(f):
        h = f.get("height") or 0
        fs = f.get("filesize") or f.get("filesize_approx") or 0
        return (h, fs)
    best = max(formats, key=score)
    return best

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

    # 1) cache check
    cached = get_cache(url)
    if cached:
        file_id, kind, size, duration = cached
        logger.info("Cache hit for %s kind=%s size=%s", url, kind, size)
        caption = build_caption(url, {"duration": duration})
        try:
            if kind == "video":
                await message.reply_video(video=file_id, caption=caption)
            elif kind == "photo":
                # photo cache stores single file_id for simplicity
                await message.reply_photo(photo=file_id, caption=caption)
            else:
                await message.reply_document(document=file_id, caption=caption)
            await status.delete()
            return
        except Exception as e:
            logger.exception("Failed to send cached file_id, will attempt fresh download: %s", e)
            # fallthrough to fresh download

    # 2) TikTok quick API (optional)
    if "tiktok.com" in url.lower():
        try:
            api_url = "https://www.tikwm.com/api/"
            r = requests.get(api_url, params={"url": url}, timeout=30)
            r.raise_for_status()
            data = r.json()
            if data.get("code") == 0:
                data = data["data"]
                # images
                if data.get("images"):
                    images = data["images"]
                    media_group = []
                    for i, img in enumerate(images):
                        media_group.append(InputMediaPhoto(media=img, caption=build_caption(url) if i == 0 else None))
                    sent = await message.reply_media_group(media_group)
                    # cache first photo file_id if available
                    try:
                        first = sent[0]
                        if first.photo:
                            fid = first.photo[-1].file_id
                            set_cache(url, fid, "photo", 0, None)
                    except Exception:
                        pass
                    await status.delete()
                    return
                # video
                video_url = data.get("hdplay") or data.get("play")
                if video_url:
                    sent = await message.reply_video(video=video_url, caption=build_caption(url))
                    try:
                        if sent.video:
                            set_cache(url, sent.video.file_id, "video", sent.video.file_size or 0, sent.video.duration or 0)
                    except Exception:
                        pass
                    await status.delete()
                    return
        except Exception as e:
            logger.exception("TikTok API fallback failed: %s", e)
            # continue to yt-dlp path

    # 3) Use yt-dlp to probe metadata (with retries)
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

    # 4) Try to handle photo-like posts (X / Twitter / Instagram)
    def extract_image_urls(info_obj: dict) -> List[str]:
        images = []
        entries = info_obj.get("entries") or []
        for e in entries:
            ext = (e.get("ext") or "").lower()
            if e.get("url") and ext in ("jpg", "jpeg", "png", "webp"):
                images.append(e["url"])
        # fallback single
        if not images and info_obj.get("url") and (info_obj.get("ext") or "").lower() in ("jpg", "jpeg", "png", "webp"):
            images.append(info_obj["url"])
        # thumbnails fallback
        if not images and info_obj.get("thumbnails"):
            for t in info_obj["thumbnails"]:
                if t.get("url"):
                    images.append(t["url"])
        return images

    image_urls = extract_image_urls(info)
    if image_urls:
        try:
            media = []
            for i, img in enumerate(image_urls):
                media.append(InputMediaPhoto(media=img, caption=build_caption(url, info) if i == 0 else None))
            sent = await message.reply_media_group(media)
            # cache first photo file_id
            try:
                first = sent[0]
                if first.photo:
                    fid = first.photo[-1].file_id
                    set_cache(url, fid, "photo", 0, info.get("duration"))
            except Exception:
                pass
            await status.delete()
            return
        except Exception as e:
            logger.exception("Failed to send photo group: %s", e)
            # continue to video path

    # 5) VIDEO PATH: download best quality, then decide to send or transcode
    # prefer to download the best available (no format restriction)
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

    # download with retries
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
    caption = build_caption(url, final_info)

    logger.info("Downloaded file %s size=%d duration=%s", filename, size, duration)

    # If file within Telegram limit, send original (max quality)
    if size <= TELEGRAM_LIMIT_BYTES:
        try:
            with open(filename, "rb") as f:
                sent = await message.reply_video(video=f, caption=caption)
            # cache
            try:
                if sent.video:
                    set_cache(url, sent.video.file_id, "video", sent.video.file_size or size, sent.video.duration or duration)
            except Exception:
                pass
            await status.delete()
            # cleanup
            try:
                os.remove(filename)
            except Exception:
                pass
            return
        except Exception as e:
            logger.exception("Failed to send original video: %s", e)
            # fallthrough to transcode attempt

    # If file too large, transcode to fit within TELEGRAM_LIMIT_BYTES
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, f"transcoded_{os.path.basename(filename)}")
            logger.info("Transcoding %s -> %s", filename, out_path)
            # run blocking transcode in thread to avoid blocking event loop
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
            # send transcoded file
            with open(out_path, "rb") as f:
                sent = await message.reply_video(video=f, caption=caption)
            try:
                if sent.video:
                    set_cache(url, sent.video.file_id, "video", sent.video.file_size or out_size, sent.video.duration or duration)
            except Exception:
                pass
            await status.delete()
            # cleanup original
            try:
                os.remove(filename)
            except Exception:
                pass
            return
    except Exception as e:
        logger.exception("Transcode/send error: %s", e)
        await status.edit_text("❌ Gate failed while optimizing the artifact.")
        # attempt to send as document if still possible (may exceed bot limits)
        try:
            await message.reply_text("Attempting to send as document (may fail if too large)...")
            with open(filename, "rb") as f:
                await message.reply_document(document=f, caption=caption)
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

