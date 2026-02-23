"""
Telegram Cloud Storage Bot
Accepts files, stores metadata in Firebase Firestore,
serves files back via Mini App integration,
and provides a web API for browser downloads.
"""

import os
import json
import asyncio
import logging

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    MenuButtonWebApp,
    WebAppInfo,
    ContentType,
)
from aiogram.filters import CommandStart

import firebase_admin
from firebase_admin import credentials, firestore

from aiohttp import web
import aiohttp as aiohttp_client

# ── Config ────────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL")
PORT = int(os.getenv("PORT", 8080))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not WEBAPP_URL:
    raise RuntimeError("WEBAPP_URL is not set")

# ── Firebase ──────────────────────────────────────────────────────────
# Supports both file-based and env-based credentials (for Render/Heroku)
firebase_creds_json = os.getenv("FIREBASE_CREDENTIALS")
if firebase_creds_json:
    cred = credentials.Certificate(json.loads(firebase_creds_json))
else:
    cred = credentials.Certificate(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "serviceAccountKey.json")
    )
firebase_admin.initialize_app(cred)
db = firestore.client()

# ── Bot / Dispatcher ─────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────

def classify_file(mime_type, file_name):
    if mime_type:
        if mime_type.startswith("image"):
            return "image"
        if mime_type.startswith(("video", "audio")):
            return "media"
    if file_name:
        ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
        if ext in ("jpg", "jpeg", "png", "gif", "bmp", "webp", "svg", "tiff"):
            return "image"
        if ext in ("mp4", "mov", "avi", "mkv", "mp3", "ogg", "wav", "flac", "aac"):
            return "media"
    return "document"


def format_size(size_bytes):
    if not size_bytes:
        return "unknown"
    s = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if s < 1024:
            return f"{s:.1f} {unit}"
        s /= 1024
    return f"{s:.1f} TB"


# ══════════════════════════════════════════════════════════════════════
#  TELEGRAM BOT HANDLERS
# ══════════════════════════════════════════════════════════════════════

@router.message(CommandStart())
async def cmd_start(message: Message):
    await bot.set_chat_menu_button(
        chat_id=message.chat.id,
        menu_button=MenuButtonWebApp(
            text="☁️ My Files",
            web_app=WebAppInfo(url=WEBAPP_URL),
        ),
    )
    await message.answer(
        "👋 <b>Welcome to Cloud Storage!</b>\n\n"
        "📤 Send me any file and I'll keep it safe.\n"
        "📂 Tap <b>☁️ My Files</b> to browse your storage.",
        parse_mode="HTML",
    )


@router.message(F.content_type.in_({
    ContentType.DOCUMENT, ContentType.PHOTO, ContentType.VIDEO,
    ContentType.AUDIO, ContentType.VOICE, ContentType.VIDEO_NOTE,
    ContentType.ANIMATION,
}))
async def handle_file(message: Message):
    chat_id = str(message.chat.id)
    progress_msg = await message.answer("⏳ <b>Saving to cloud...</b>", parse_mode="HTML")

    file_id = file_name = file_size = mime_type = None

    if message.document:
        file_id, file_name = message.document.file_id, message.document.file_name or "document"
        file_size, mime_type = message.document.file_size, message.document.mime_type
    elif message.photo:
        photo = message.photo[-1]
        file_id, file_name = photo.file_id, f"photo_{photo.file_unique_id}.jpg"
        file_size, mime_type = photo.file_size, "image/jpeg"
    elif message.video:
        file_id, file_name = message.video.file_id, message.video.file_name or "video.mp4"
        file_size, mime_type = message.video.file_size, message.video.mime_type or "video/mp4"
    elif message.audio:
        file_id, file_name = message.audio.file_id, message.audio.file_name or "audio.mp3"
        file_size, mime_type = message.audio.file_size, message.audio.mime_type or "audio/mpeg"
    elif message.voice:
        file_id, file_name = message.voice.file_id, f"voice_{message.message_id}.ogg"
        file_size, mime_type = message.voice.file_size, "audio/ogg"
    elif message.video_note:
        file_id, file_name = message.video_note.file_id, f"videonote_{message.message_id}.mp4"
        file_size, mime_type = message.video_note.file_size, "video/mp4"
    elif message.animation:
        file_id, file_name = message.animation.file_id, message.animation.file_name or "animation.gif"
        file_size, mime_type = message.animation.file_size, message.animation.mime_type or "image/gif"

    if not file_id:
        await progress_msg.edit_text("❌ Could not process this file.")
        return

    category = classify_file(mime_type, file_name)
    doc_data = {
        "file_id": file_id, "file_name": file_name, "file_size": file_size,
        "mime_type": mime_type, "category": category, "chat_id": chat_id,
        "timestamp": firestore.SERVER_TIMESTAMP,
    }

    try:
        db.collection("files").add(doc_data)
        await progress_msg.edit_text(
            f"✅ <b>Saved to cloud!</b>\n\n"
            f"📄 <b>{file_name}</b>\n"
            f"📦 {format_size(file_size)} · {category.title()}\n\n"
            f"Open <b>☁️ My Files</b> to see it.",
            parse_mode="HTML",
        )
        logger.info(f"Saved '{file_name}' for chat {chat_id}")
    except Exception as e:
        logger.error(f"Firestore write error: {e}")
        await progress_msg.edit_text(f"❌ <b>Failed to save</b>\n\n{e}", parse_mode="HTML")


@router.message(F.web_app_data)
async def handle_webapp_data(message: Message):
    data = message.web_app_data.data.strip()
    chat_id = message.chat.id

    if not data:
        await message.answer("⚠️ No data received.")
        return
    if data == "__UPLOAD__":
        await message.answer(
            "📤 <b>Upload a file</b>\n\nSend me any file right here!",
            parse_mode="HTML",
        )
        return

    # Try to send file back using file_id
    file_id = data
    sent = False
    for method in [bot.send_document, bot.send_photo, bot.send_video, bot.send_audio]:
        try:
            await method(chat_id=chat_id, **{method.__name__.replace("send_", ""): file_id})
            sent = True
            break
        except Exception:
            continue
    if not sent:
        await message.answer("❌ Could not send the file. It may have expired.")


# ══════════════════════════════════════════════════════════════════════
#  WEB API (aiohttp)
# ══════════════════════════════════════════════════════════════════════

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}


async def handle_index(request):
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "webapp", "index.html")
    if os.path.exists(html_path):
        return web.FileResponse(html_path)
    return web.Response(text="Cloud Storage Bot is running!", content_type="text/html")


async def handle_health(request):
    return web.json_response({"status": "ok", "bot": "running"}, headers=CORS_HEADERS)


async def handle_api_files(request):
    if request.method == "OPTIONS":
        return web.Response(headers=CORS_HEADERS)
    try:
        docs = db.collection("files").stream()
        files = []
        for doc in docs:
            d = doc.to_dict()
            d["id"] = doc.id
            if d.get("timestamp"):
                d["timestamp"] = {"seconds": int(d["timestamp"].timestamp())}
            files.append(d)
        files.sort(key=lambda x: x.get("timestamp", {}).get("seconds", 0), reverse=True)
        return web.json_response(files, headers=CORS_HEADERS)
    except Exception as e:
        logger.error(f"API /files error: {e}")
        return web.json_response({"error": str(e)}, status=500, headers=CORS_HEADERS)


async def handle_api_download(request):
    """Download file from Telegram. Note: Telegram Bot API limits getFile to 20MB."""
    if request.method == "OPTIONS":
        return web.Response(headers=CORS_HEADERS)

    file_id = request.query.get("file_id")
    file_name = request.query.get("name", "file")
    if not file_id:
        return web.json_response({"error": "file_id required"}, status=400, headers=CORS_HEADERS)

    try:
        tg_file = await bot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_file.file_path}"

        async with aiohttp_client.ClientSession() as session:
            async with session.get(file_url) as resp:
                if resp.status != 200:
                    return web.json_response(
                        {"error": "Telegram download failed"}, status=502, headers=CORS_HEADERS
                    )
                content = await resp.read()
                ct = resp.content_type or "application/octet-stream"
                return web.Response(
                    body=content,
                    headers={
                        **CORS_HEADERS,
                        "Content-Disposition": f'attachment; filename="{file_name}"',
                        "Content-Type": ct,
                    },
                )
    except Exception as e:
        err_msg = str(e)
        if "file is too big" in err_msg.lower():
            return web.json_response(
                {"error": "File is larger than 20MB. Use the Telegram bot to download it (Get button in Mini App)."},
                status=413, headers=CORS_HEADERS
            )
        logger.error(f"Download error: {e}")
        return web.json_response({"error": err_msg}, status=500, headers=CORS_HEADERS)


async def handle_api_send(request):
    """Send a file back to user via bot (works for ANY file size)."""
    if request.method == "OPTIONS":
        return web.Response(headers=CORS_HEADERS)

    try:
        data = await request.json()
        file_id = data.get("file_id")
        chat_id = data.get("chat_id")
        if not file_id or not chat_id:
            return web.json_response({"error": "file_id and chat_id required"}, status=400, headers=CORS_HEADERS)

        sent = False
        for method in [bot.send_document, bot.send_photo, bot.send_video, bot.send_audio]:
            try:
                await method(chat_id=int(chat_id), **{method.__name__.replace("send_", ""): file_id})
                sent = True
                break
            except Exception:
                continue

        if sent:
            return web.json_response({"ok": True}, headers=CORS_HEADERS)
        else:
            return web.json_response({"error": "Could not send file"}, status=500, headers=CORS_HEADERS)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500, headers=CORS_HEADERS)


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════

async def main():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_route("*", "/api/files", handle_api_files)
    app.router.add_route("*", "/api/download", handle_api_download)
    app.router.add_route("*", "/api/send", handle_api_send)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 Web server on port {PORT}")

    logger.info("🤖 Bot starting…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
