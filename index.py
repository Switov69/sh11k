import os
import json
import uuid

from fastapi import FastAPI, Request, Response
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    Update, Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto, InputMediaVideo,
)
from aiogram.filters import Command
from aiogram.enums import ParseMode
import redis.asyncio as aioredis

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
ADMIN_ID: int = int(os.environ["ADMIN_ID"])
CHANNEL_ID: str = os.environ["CHANNEL_ID"]
REDIS_URL: str = os.environ["REDIS_URL"]

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
app = FastAPI()


def get_redis() -> aioredis.Redis:
    return aioredis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=4,
        socket_timeout=4,
    )


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    r = get_redis()
    try:
        await r.set(f"user:{message.from_user.id}:state", "waiting_content", ex=3600)
    finally:
        await r.aclose()

    await message.answer(
        "👋 <b>Привет!</b>\n\n"
        "Отправь фото или видео (одно или альбом до 2-х штук) для публикации.\n\n"
        "📎 <b>Требования:</b>\n"
        "• Обязательно добавь подпись к медиа\n"
        "• Максимум <b>2 медиафайла</b>\n"
        "• Можно: 1 фото, 1 видео, 2 фото, 2 видео или микс\n\n"
        "⚠️ <b>Лимиты:</b> Фото до 10МБ, Видео до 50МБ."
    )


async def _process_submission(message: Message, data: dict) -> None:
    """Общая функция отправки на модерацию"""
    post_id = uuid.uuid4().hex[:12]

    r = get_redis()
    try:
        await r.set(f"post:{post_id}", json.dumps(data), ex=86400 * 7)
    finally:
        await r.aclose()

    caption = data["caption"]
    user_id = data["user_id"]
    username = data["username"]
    first_name = data["first_name"]

    media_list = await _build_media_list(data["media"], caption)
    
    # Если медиа одно - отправляем простым методом, если больше - группой
    if len(media_list) > 1:
        await bot.send_media_group(chat_id=ADMIN_ID, media=media_list)
    else:
        m = media_list[0]
        if isinstance(m, InputMediaPhoto):
            await bot.send_photo(chat_id=ADMIN_ID, photo=m.media, caption=m.caption)
        else:
            await bot.send_video(chat_id=ADMIN_ID, video=m.media, caption=m.caption)

    username_display = f"@{username}" if username else f"#{user_id}"
    profile_link = f'<a href="tg://user?id={user_id}">{first_name}</a>'

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{post_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{post_id}"),
            ]
        ]
    )

    await bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"<b>Новый пост на модерацию</b>\n\n"
            f"{username_display} | <code>{user_id}</code> | {profile_link}"
        ),
        reply_markup=keyboard,
    )

    await message.answer("✅ Твой пост отправлен на модерацию!")


@dp.message(F.media_group_id)
async def handle_media_group(message: Message) -> None:
    """Обработка альбомов (2 файла)"""
    user_id = message.from_user.id
    media_group_id = message.media_group_id
    group_key = f"media_group:{media_group_id}"
    
    r = get_redis()
    try:
        state = await r.get(f"user:{user_id}:state")
        if state != "waiting_content": return

        file_id = message.photo[-1].file_id if message.photo else message.video.file_id
        file_type = "photo" if message.photo else "video"
        caption = message.caption or ""

        raw = await r.get(group_key)
        group_data = json.loads(raw) if raw else {
            "user_id": user_id, "username": message.from_user.username or "",
            "first_name": message.from_user.first_name or "",
            "caption": caption, "media": [],
        }

        if caption and not group_data["caption"]:
            group_data["caption"] = caption

        # Добавляем файл, если его еще нет
        if not any(m["file_id"] == file_id for m in group_data["media"]):
            group_data["media"].append({"file_id": file_id, "type": file_type})

        # Если набрали 2 файла или это последний элемент (в TG альбомы прилетают быстро)
        if len(group_data["media"]) >= 2:
            await r.delete(f"user:{user_id}:state")
            await r.delete(group_key)
            if not group_data["caption"]:
                await message.answer("❌ Забыл подпись! Отправь альбом снова с текстом.")
                return
            await _process_submission(message, group_data)
        else:
            # Ждем второй файл
            await r.set(group_key, json.dumps(group_data), ex=30)
    finally:
        await r.aclose()


@dp.message(F.photo | F.video)
async def handle_single_media(message: Message) -> None:
    """Обработка одиночных фото/видео"""
    if message.media_group_id: return # Пропускаем, если это часть альбома

    user_id = message.from_user.id
    r = get_redis()
    try:
        state = await r.get(f"user:{user_id}:state")
        if state != "waiting_content": return

        if not message.caption:
            await message.answer("❌ Добавь описание к фото/видео.")
            return

        file_id = message.photo[-1].file_id if message.photo else message.video.file_id
        file_type = "photo" if message.photo else "video"

        data = {
            "user_id": user_id,
            "username": message.from_user.username or "",
            "first_name": message.from_user.first_name or "",
            "caption": message.caption,
            "media": [{"file_id": file_id, "type": file_type}],
        }
        
        await r.delete(f"user:{user_id}:state")
        await _process_submission(message, data)
    finally:
        await r.aclose()


@dp.message(F.text)
async def handle_plain_text(message: Message) -> None:
    if message.text.startswith("/"): return
    await message.answer("❌ Пришли фото или видео с описанием. Просто текст не принимается.")


async def _build_media_list(media: list[dict], caption: str) -> list:
    result = []
    for i, item in enumerate(media):
        cap = caption if i == 0 else None
        if item["type"] == "photo":
            result.append(InputMediaPhoto(media=item["file_id"], caption=cap, parse_mode=ParseMode.HTML))
        else:
            result.append(InputMediaVideo(media=item["file_id"], caption=cap, parse_mode=ParseMode.HTML))
    return result


@dp.callback_query(F.data.startswith("approve:"))
async def approve_post(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return

    post_id = callback.data.split(":", 1)[1]
    r = get_redis()
    try:
        raw = await r.get(f"post:{post_id}")
        if not raw:
            await callback.answer("Пост не найден.", show_alert=True)
            return
        data = json.loads(raw)
        await r.delete(f"post:{post_id}")
    finally:
        await r.aclose()

    media_list = await _build_media_list(data["media"], data["caption"])
    
    if len(media_list) > 1:
        await bot.send_media_group(chat_id=CHANNEL_ID, media=media_list)
    else:
        m = media_list[0]
        if isinstance(m, InputMediaPhoto):
            await bot.send_photo(chat_id=CHANNEL_ID, photo=m.media, caption=m.caption)
        else:
            await bot.send_video(chat_id=CHANNEL_ID, video=m.media, caption=m.caption)

    await bot.send_message(chat_id=data["user_id"], text="🎉 Твой пост опубликован!")
    await callback.message.edit_text(callback.message.text + "\n\n✅ <b>Опубликовано.</b>")


@dp.callback_query(F.data.startswith("reject:"))
async def reject_post(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID: return
    post_id = callback.data.split(":", 1)[1]
    r = get_redis()
    try:
        raw = await r.get(f"post:{post_id}")
        if raw:
            data = json.loads(raw)
            await bot.send_message(chat_id=data["user_id"], text="❌ Пост отклонён.")
            await r.delete(f"post:{post_id}")
    finally:
        await r.aclose()
    await callback.message.edit_text(callback.message.text + "\n\n❌ <b>Отклонено.</b>")


@app.post("/webhook")
async def webhook(request: Request) -> Response:
    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot=bot, update=update)
    return Response(content="ok")


@app.get("/")
async def healthcheck() -> dict:
    return {"status": "ok"}
