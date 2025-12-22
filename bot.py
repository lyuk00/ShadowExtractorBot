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
import aiohttp
from flask import Flask
from telegram import Update, InputMediaPhoto, Bot
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
    "<b>🗡️ Solo Leveling Gate</b>\n"
    "<i>Source</i>: <a href=\"{url}\">open</a>\n"
    "⏱️ <b>Duration</b>: {duration}s   🎚️ <b>Quality</b>: {height}p\n"
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
logger = logging.getLogger("solo_leveling_gate")

# ===============================
# FLASK KEEPALIVE
# ===============================
app = Flask(__name__)

@app.route("/")
def home():
    return "Solo Leveling Gate online 🗡️", 200

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
direct_url_cache: Dict[str, Dict] = {}  # url -> {"direct": direct_url, "file_id": file_id, "size": size}

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

def transcode_high_quality(input_path: str, output_path: str, max_bytes: int, max_width=1920, max_height=1080) -> str:
    """
    Robust transcode: tries AAC, falls back to MP3 if needed.
    """
    crf = 18
    while True:
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-c:v", "libx264",
            "-preset", "slow",
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-maxrate", "8M",
            "-bufsize", "16M",
            "-vf", f"scale='min({max_width},iw)':'min({max_height},ih)':force_original_aspect_ratio=decrease",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-ac", "2",
            "-movflags", "+faststart",
            output_path
        ]
        logger.info("Running ffmpeg (CRF=%s)", crf)
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode(errors="ignore") if e.stderr else ""
            logger.error("ffmpeg failed (CRF=%s): %s", crf, stderr)
            if "Unknown encoder 'aac'" in stderr or "Error while opening encoder for output stream #0:0" in stderr or "Invalid argument" in stderr:
                logger.info("Attempting audio fallback to libmp3lame")
                cmd_fallback = cmd.copy()
                for i, v in enumerate(cmd_fallback):
                    if v == "-c:a" and i + 1 < len(cmd_fallback):
                        cmd_fallback[i+1] = "libmp3lame"
                        break
                try:
                    subprocess.run(cmd_fallback, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    size = os.path.getsize(output_path)
                    logger.info("Fallback mp3 succeeded, size=%d", size)
                    if size <= max_bytes or crf >= 28:
                        return output_path
                    crf += 2
                    continue
                except subprocess.CalledProcessError as e2:
                    stderr2 = e2.stderr.decode(errors="ignore") if e2.stderr else ""
                    logger.error("Fallback mp3 failed: %s", stderr2)
                    raise
            raise
        size = os.path.getsize(output_path)
        logger.info("Transcoded size: %d bytes (limit %d)", size, max_bytes)
        if size <= max_bytes or crf >= 28:
            return output_path
        crf += 2

# ===============================
# FAST-PATH HELPERS (direct send)
# ===============================
def choose_direct_format(info: dict, max_bytes: int) -> Optional[dict]:
    formats = info.get("formats") or []
    candidates = []
    for f in formats:
        url = f.get("url")
        if not url:
            continue
        vcodec = (f.get("vcodec") or "").lower()
        # accept formats with video or merged streams
        if vcodec == "none" and not f.get("acodec"):
            continue
        # prefer mp4 containers slightly
        candidates.append(f)
    if not candidates:
        return None

    def score(f):
        h = f.get("height") or 0
        fs = f.get("filesize") or f.get("filesize_approx") or 0
        return (int(h), -int(fs or 0))

    candidates.sort(key=score, reverse=True)

    for f in candidates:
        fs = f.get("filesize") or f.get("filesize_approx")
        if fs and fs <= max_bytes:
            return f

    return candidates[0]

async def head_content_length(url: str, timeout: int = 8) -> Optional[int]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(url, timeout=timeout, allow_redirects=True) as resp:
                if resp.status in (200, 206):
                    cl = resp.headers.get("Content-Length")
                    if cl:
                        return int(cl)
    except Exception as e:
        logger.debug("HEAD check failed for %s: %s", url, e)
    return None

# ===============================
# BACKGROUND PROCESSOR
# ===============================
async def process_url_in_background(bot: Bot, chat_id: int, status_message_id: int, url: str, user_id: int):
    """
    Background worker: performs the heavy lifting and edits the status message with progress.
    """
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                    text="🗡️ Gate opened — scanning the realm...", parse_mode="HTML")
    except Exception:
        pass

    # 1) cache check
    cached = get_cache(url)
    if cached:
        file_id, kind, size, duration = cached
        caption = build_caption_html(url, {"duration": duration})
        try:
            if kind == "video":
                await bot.send_video(chat_id=chat_id, video=file_id, caption=caption, parse_mode="HTML")
            elif kind == "photo":
                await bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption, parse_mode="HTML")
            else:
                await bot.send_document(chat_id=chat_id, document=file_id, caption=caption, parse_mode="HTML")
            await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                        text="✅ Gate delivered from cache.", parse_mode="HTML")
            return
        except Exception as e:
            logger.exception("Failed to send cached file_id in background: %s", e)
            # continue to fresh download

    # TikTok quick API attempt
    if "tiktok.com" in url.lower():
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                        text="🗡️ Scanning TikTok realm...", parse_mode="HTML")
            api_url = "https://www.tikwm.com/api/"
            r = requests.get(api_url, params={"url": url}, timeout=30)
            r.raise_for_status()
            data = r.json()
            if data.get("code") == 0:
                data = data["data"]
                if data.get("images"):
                    images = data["images"]
                    for i, img in enumerate(images):
                        if i == 0:
                            await bot.send_photo(chat_id=chat_id, photo=img, caption=build_caption_html(url, None), parse_mode="HTML")
                        else:
                            await bot.send_photo(chat_id=chat_id, photo=img)
                    await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                                text="✅ Gate delivered (images).", parse_mode="HTML")
                    return
                video_url = data.get("hdplay") or data.get("play")
                if video_url:
                    await bot.send_video(chat_id=chat_id, video=video_url, caption=build_caption_html(url, None), parse_mode="HTML")
                    await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                                text="✅ Gate delivered (TikTok video).", parse_mode="HTML")
                    return
        except Exception as e:
            logger.exception("TikTok API fallback failed in background: %s", e)

    # 2) yt-dlp probe
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
            logger.info("Background: probe metadata (attempt %d) for %s", attempt + 1, url)
            with yt_dlp.YoutubeDL(ydl_opts_probe) as ydl:
                info = ydl.extract_info(url, download=False)
            break
        except yt_dlp.utils.DownloadError as de:
            msg = str(de)
            logger.exception("yt-dlp DownloadError during probe: %s", msg)
            if "Sign in to confirm you’re not a bot" in msg or "use --cookies" in msg.lower() or "cookies" in msg.lower():
                text = (
                    "❌ Gate blocked by source (login/cookies required).\n\n"
                    "This content requires authentication. To fix:\n"
                    "• Export a cookies.txt from your browser (see yt-dlp docs).\n"
                    "• Set the environment variable COOKIEFILE to the path of cookies.txt in your host/container.\n\n"
                    "Example: set COOKIEFILE=/app/cookies.txt and redeploy."
                )
                try:
                    await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id, text=text)
                except Exception:
                    pass
                return
            if attempt < MAX_DOWNLOAD_RETRIES:
                await asyncio.sleep(1 + attempt * 2)
                continue
            else:
                try:
                    await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                               text="❌ Gate collapsed while scanning metadata.", parse_mode="HTML")
                except Exception:
                    pass
                return
        except Exception as e:
            logger.exception("yt-dlp probe error in background: %s", e)
            if attempt < MAX_DOWNLOAD_RETRIES:
                await asyncio.sleep(1 + attempt * 2)
                continue
            else:
                try:
                    await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                               text="❌ Gate collapsed while scanning metadata.", parse_mode="HTML")
                except Exception:
                    pass
                return

    if not info:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                       text="❌ No metadata found for this gate.", parse_mode="HTML")
        except Exception:
            pass
        return

    # FAST PATH: try to send direct URL without downloading
    try:
        best_format = choose_direct_format(info, TELEGRAM_LIMIT_BYTES)
        if best_format:
            direct_url = best_format.get("url")
            fs = best_format.get("filesize") or best_format.get("filesize_approx")
            if fs and fs <= TELEGRAM_LIMIT_BYTES:
                await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                            text="🗡️ Delivering artifact (direct link, no download)...", parse_mode="HTML")
                sent = await bot.send_video(chat_id=chat_id, video=direct_url, caption=build_caption_html(url, info), parse_mode="HTML")
                try:
                    if sent.video:
                        set_cache(url, sent.video.file_id, "video", sent.video.file_size or fs, sent.video.duration or info.get("duration"))
                except Exception:
                    pass
                await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                            text="✅ Gate delivered (direct).", parse_mode="HTML")
                return
            else:
                if direct_url:
                    head_size = await head_content_length(direct_url)
                    if head_size and head_size <= TELEGRAM_LIMIT_BYTES:
                        await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                                    text="🗡️ Delivering artifact (direct link, HEAD check)...", parse_mode="HTML")
                        sent = await bot.send_video(chat_id=chat_id, video=direct_url, caption=build_caption_html(url, info), parse_mode="HTML")
                        try:
                            if sent.video:
                                set_cache(url, sent.video.file_id, "video", sent.video.file_size or head_size, sent.video.duration or info.get("duration"))
                        except Exception:
                            pass
                        await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                                    text="✅ Gate delivered (direct HEAD).", parse_mode="HTML")
                        return
                    else:
                        logger.info("Direct URL HEAD size unknown or too large (%s), falling back to download", head_size)
    except Exception as e:
        logger.exception("Fast-path direct send failed, falling back to download: %s", e)

    # images vs video decision
    if not has_video_in_formats(info):
        image_urls = extract_image_urls(info)
        if image_urls:
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                            text="🗡️ Delivering images...", parse_mode="HTML")
                for i, img in enumerate(image_urls):
                    if i == 0:
                        await bot.send_photo(chat_id=chat_id, photo=img, caption=build_caption_html(url, info), parse_mode="HTML")
                    else:
                        await bot.send_photo(chat_id=chat_id, photo=img)
                await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                            text="✅ Gate delivered (images).", parse_mode="HTML")
                return
            except Exception as e:
                logger.exception("Background image send failed: %s", e)
                # fallback to video path

    # VIDEO PATH: download best quality
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
            await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                        text=f"🗡️ Downloading artifact (attempt {attempt+1})...", parse_mode="HTML")
            with yt_dlp.YoutubeDL(ydl_opts_dl) as ydl:
                final_info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(final_info)
            break
        except yt_dlp.utils.DownloadError as de:
            msg = str(de)
            logger.exception("yt-dlp DownloadError during download: %s", msg)
            if "Sign in to confirm you’re not a bot" in msg or "use --cookies" in msg.lower() or "cookies" in msg.lower():
                text = (
                    "❌ Gate blocked by source (login/cookies required).\n\n"
                    "This content requires authentication. To fix:\n"
                    "• Export a cookies.txt from your browser (see yt-dlp docs).\n"
                    "• Set the environment variable COOKIEFILE to the path of cookies.txt in your host/container.\n\n"
                    "Example: set COOKIEFILE=/app/cookies.txt and redeploy."
                )
                try:
                    await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id, text=text)
                except Exception:
                    pass
                return
            if attempt < MAX_DOWNLOAD_RETRIES:
                await asyncio.sleep(1 + attempt * 2)
                continue
            else:
                try:
                    await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                               text="❌ Gate collapsed while downloading artifact.", parse_mode="HTML")
                except Exception:
                    pass
                return
        except Exception as e:
            logger.exception("yt-dlp download error in background: %s", e)
            if attempt < MAX_DOWNLOAD_RETRIES:
                await asyncio.sleep(1 + attempt * 2)
                continue
            else:
                try:
                    await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                               text="❌ Gate collapsed while downloading artifact.", parse_mode="HTML")
                except Exception:
                    pass
                return

    if not filename or not os.path.exists(filename):
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                       text="❌ Artifact not produced by the gate.", parse_mode="HTML")
        except Exception:
            pass
        return

    size = os.path.getsize(filename)
    duration = final_info.get("duration") if final_info else None
    caption = build_caption_html(url, final_info)

    logger.info("Background: downloaded file %s size=%d duration=%s", filename, size, duration)

    # send original if within limit
    if size <= TELEGRAM_LIMIT_BYTES:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                        text="🗡️ Sending artifact (original quality)...", parse_mode="HTML")
            with open(filename, "rb") as f:
                await bot.send_video(chat_id=chat_id, video=f, caption=caption, parse_mode="HTML")
            await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                        text="✅ Gate delivered (original quality).", parse_mode="HTML")
            try:
                os.remove(filename)
            except Exception:
                pass
            return
        except Exception as e:
            logger.exception("Background failed to send original video: %s", e)
            # continue to transcode

    # transcode if too large
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                    text="🗡️ Optimizing artifact for delivery (transcoding)...", parse_mode="HTML")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, f"transcoded_{os.path.basename(filename)}")
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, transcode_high_quality, filename, out_path, TELEGRAM_LIMIT_BYTES, 1920, 1080)
            if not os.path.exists(out_path):
                await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                           text="❌ Gate failed during transcode.", parse_mode="HTML")
                return
            out_size = os.path.getsize(out_path)
            if out_size > TELEGRAM_LIMIT_BYTES:
                await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                           text="❌ Artifact too heavy even after transcode.", parse_mode="HTML")
                return
            with open(out_path, "rb") as f:
                await bot.send_video(chat_id=chat_id, video=f, caption=caption, parse_mode="HTML")
            await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                        text="✅ Gate delivered (optimized).", parse_mode="HTML")
            try:
                os.remove(filename)
            except Exception:
                pass
            return
    except Exception as e:
        logger.exception("Background transcode/send error: %s", e)
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                       text="❌ Gate failed while optimizing the artifact.", parse_mode="HTML")
        except Exception:
            pass
        # attempt to send as document
        try:
            await bot.send_message(chat_id=chat_id, text="Attempting to send as document (may fail if too large)...")
            with open(filename, "rb") as f:
                await bot.send_document(chat_id=chat_id, document=f, caption=caption, parse_mode="HTML")
            await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                        text="✅ Gate delivered (document).", parse_mode="HTML")
            return
        except Exception as e2:
            logger.exception("Final document send failed: %s", e2)
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                           text="❌ Final gate attempt failed.", parse_mode="HTML")
            except Exception:
                pass
            return

# ===============================
# TELEGRAM HANDLERS
# ===============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🗡️ Solo Leveling Gate online. Send a supported link to open the gate.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📜 Solo Leveling Gate - How to use:\n"
        "- Send a public link (YouTube, Instagram, X, TikTok).\n"
        "- The bot will try to deliver the artifact in highest quality, fast.\n"
        "- If content requires login, provide cookies via COOKIEFILE env var.\n"
        "Commands: /start /help /ping /stats"
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🗡️ Gate heartbeat: OK")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT COUNT(*) FROM cache")
    total = cur.fetchone()[0]
    conn.close()
    await update.message.reply_text(f"🗡️ Cache entries: {total}")

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

    logger.info("Incoming gate request: %s from %s", url, message.from_user.id)

    # Immediate acknowledgement: fast response to user
    try:
        status = await message.reply_text("🗡️ Gate opening... Preparing to scan the realm.", parse_mode="HTML")
    except Exception:
        status = await message.reply_text("Gate opening...")

    # Launch background processing and return immediately
    try:
        bot = context.bot
        chat_id = message.chat_id
        status_message_id = status.message_id
        user_id = message.from_user.id
        asyncio.create_task(process_url_in_background(bot, chat_id, status_message_id, url, user_id))
    except Exception as e:
        logger.exception("Failed to start background task: %s", e)
        try:
            await status.edit_text("❌ Failed to start gate processing.")
        except Exception:
            pass

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
