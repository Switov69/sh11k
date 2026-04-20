import os
import json
import uuid

from fastapi import FastAPI, Request, Response
from aiogram import Bot, Dispatcher, F
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

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
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
        "Отправь сообщение с медиафайлами для публикации в канале.\n\n"
        "📎 <b>Обязательные требования:</b>\n"
        "• Подпись-описание к медиафайлам (обязательно)\n"
        "• Ровно <b>2 фото</b> или <b>2 видео</b> одним альбомом\n\n"
        "⚠️ <b>Лимиты Telegram:</b>\n"
        "• Фото — до <b>10 МБ</b> каждое\n"
        "• Видео — до <b>50 МБ</b> каждое\n\n"
        "Выбери один тип: только фото <i>или</i> только видео."
    )


@dp.message(F.media_group_id)
async def handle_media_group(message: Message) -> None:
    user_id = message.from_user.id
    media_group_id = message.media_group_id
    group_key = f"media_group:{media_group_id}"
    completed_data: dict | None = None

    r = get_redis()
    try:
        state = await r.get(f"user:{user_id}:state")
        if state != "waiting_content":
            return

        if message.photo:
            media_type = "photo"
            file_id = message.photo[-1].file_id
        elif message.video:
            media_type = "video"
            file_id = message.video.file_id
        else:
            return

        caption = message.caption or ""

        raw = await r.get(group_key)
        if raw:
            group_data = json.loads(raw)
        else:
            group_data = {
                "user_id": user_id,
                "username": message.from_user.username or "",
                "first_name": message.from_user.first_name or "",
                "media_type": media_type,
                "caption": caption,
                "file_ids": [],
            }

        if caption and not group_data["caption"]:
            group_data["caption"] = caption

        if file_id not in group_data["file_ids"]:
            group_data["file_ids"].append(file_id)

        if len(group_data["file_ids"]) < 2:
            await r.set(group_key, json.dumps(group_data), ex=30)
            return

        await r.delete(f"user:{user_id}:state")
        await r.delete(group_key)
        completed_data = group_data

    finally:
        await r.aclose()

    if completed_data is None:
        return

    if not completed_data["caption"]:
        r2 = get_redis()
        try:
            await r2.set(f"user:{user_id}:state", "waiting_content", ex=3600)
        finally:
            await r2.aclose()
        await message.answer(
            "❌ Ты не добавил подпись к медиафайлам.\n"
            "Отправь альбом снова и обязательно добавь текст-описание."
        )
        return

    await _process_submission(message, completed_data)


@dp.message(~F.media_group_id & (F.photo | F.video))
async def handle_single_media(message: Message) -> None:
    r = get_redis()
    try:
        state = await r.get(f"user:{message.from_user.id}:state")
    finally:
        await r.aclose()
    if state == "waiting_content":
        await message.answer(
            "❌ Нужно отправить ровно <b>2 фото</b> или <b>2 видео</b> <u>одним альбомом</u>.\n"
            "Выдели оба файла и отправь их вместе."
        )


@dp.message(F.text)
async def handle_plain_text(message: Message) -> None:
    if message.text.startswith("/"):
        return
    r = get_redis()
    try:
        state = await r.get(f"user:{message.from_user.id}:state")
    finally:
        await r.aclose()
    if state == "waiting_content":
        await message.answer(
            "❌ Текст без медиафайлов не принимается.\n"
            "Прикрепи <b>2 фото</b> или <b>2 видео</b> альбомом и добавь текст в подпись."
        )


async def _process_submission(message: Message, data: dict) -> None:
    post_id = uuid.uuid4().hex[:12]

    r = get_redis()
    try:
        await r.set(f"post:{post_id}", json.dumps(data), ex=86400 * 7)
    finally:
        await r.aclose()

    media_type = data["media_type"]
    file_ids = data["file_ids"][:2]
    caption = data["caption"]
    user_id = data["user_id"]
    username = data["username"]
    first_name = data["first_name"]

    if media_type == "photo":
        media_list = [
            InputMediaPhoto(media=file_ids[0], caption=caption, parse_mode=ParseMode.HTML),
            InputMediaPhoto(media=file_ids[1]),
        ]
    else:
        media_list = [
            InputMediaVideo(media=file_ids[0], caption=caption, parse_mode=ParseMode.HTML),
            InputMediaVideo(media=file_ids[1]),
        ]

    await bot.send_media_group(chat_id=ADMIN_ID, media=media_list)

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

    await message.answer(
        "✅ Твой пост отправлен на модерацию!\n"
        "Ожидай решения администратора — мы уведомим тебя."
    )


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
            await callback.answer("Пост не найден или уже обработан.", show_alert=True)
            return
        data = json.loads(raw)
        await r.delete(f"post:{post_id}")
    finally:
        await r.aclose()

    media_type = data["media_type"]
    file_ids = data["file_ids"][:2]
    caption = data["caption"]
    user_id = data["user_id"]

    if media_type == "photo":
        media_list = [
            InputMediaPhoto(media=file_ids[0], caption=caption, parse_mode=ParseMode.HTML),
            InputMediaPhoto(media=file_ids[1]),
        ]
    else:
        media_list = [
            InputMediaVideo(media=file_ids[0], caption=caption, parse_mode=ParseMode.HTML),
            InputMediaVideo(media=file_ids[1]),
        ]

    await bot.send_media_group(chat_id=CHANNEL_ID, media=media_list)
    await bot.send_message(
        chat_id=user_id,
        text="🎉 Твой пост одобрен и опубликован в канале!",
    )

    await callback.message.edit_text(
        callback.message.text + "\n\n✅ <b>Одобрено и опубликовано.</b>",
        reply_markup=None,
    )
    await callback.answer("✅ Пост опубликован в канале!")


@dp.callback_query(F.data.startswith("reject:"))
async def reject_post(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return

    post_id = callback.data.split(":", 1)[1]

    r = get_redis()
    try:
        raw = await r.get(f"post:{post_id}")
        if not raw:
            await callback.answer("Пост не найден или уже обработан.", show_alert=True)
            return
        data = json.loads(raw)
        await r.delete(f"post:{post_id}")
    finally:
        await r.aclose()

    user_id = data["user_id"]
    await bot.send_message(
        chat_id=user_id,
        text="❌ К сожалению, твой пост был отклонён администратором.",
    )

    await callback.message.edit_text(
        callback.message.text + "\n\n❌ <b>Отклонено.</b>",
        reply_markup=None,
    )
    await callback.answer("❌ Пост отклонён.")


@app.post("/webhook")
async def webhook(request: Request) -> Response:
    try:
        data = await request.json()
        update = Update(**data)
        await dp.feed_update(bot=bot, update=update)
    except Exception:
        pass
    return Response(content="ok", status_code=200)


@app.get("/")
async def healthcheck() -> dict:
    return {"status": "ok"}
