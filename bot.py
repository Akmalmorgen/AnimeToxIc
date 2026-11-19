import asyncio
import os
import re
import tempfile
import shutil
from pathlib import Path

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from aiotracemoeapi import TraceMoe
from shazamio import Shazam, Serialize
import yt_dlp

# ---------- КОНФИГУРАЦИЯ ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")  # Строка подключения к Neon

# ---------- ИНИЦИАЛИЗАЦИЯ БОТА ----------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ---------- ПУЛ СОЕДИНЕНИЙ С БД ----------
db_pool: asyncpg.Pool = None

async def init_db():
    """Создание таблиц при первом запуске."""
    async with db_pool.acquire() as conn:
        # Таблица пользователей
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        # Таблица для хранения настроек (ключ-значение)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value JSONB
            );
        """)
    print("✅ База данных инициализирована.")

async def get_setting(key: str):
    """Получить значение настройки из БД."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = $1", key)
        return row["value"] if row else None

async def set_setting(key: str, value):
    """Сохранить значение настройки в БД."""
    import json
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO settings (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = $2
        """, key, json.dumps(value))

async def add_user(user_id: int, username: str = None, first_name: str = None):
    """Добавить пользователя, если его еще нет."""
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username, first_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO NOTHING
        """, user_id, username, first_name)

async def get_all_users():
    """Получить список ID всех пользователей для рассылки."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM users")
        return [row["user_id"] for row in rows]

# ---------- СОСТОЯНИЯ (FSM) ----------
class AdminStates(StatesGroup):
    main_menu = State()
    sub_button_name = State()
    sub_channel_link = State()
    sub_photo = State()
    broadcast_choice = State()
    broadcast_text = State()
    broadcast_photo = State()
    broadcast_preview = State()

# ---------- ЦВЕТНЫЕ КНОПКИ (ХЕЛПЕР) ----------
def btn(text, callback_data=None, url=None, style=None):
    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        url=url,
        style=style  # 'primary', 'success', 'danger'
    )

# ---------- MIDDLEWARE: ОБЯЗАТЕЛЬНАЯ ПОДПИСКА ----------
class SubscriptionMiddleware:
    async def __call__(self, handler, event, data):
        user_id = None
        if isinstance(event, Message):
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery):
            user_id = event.from_user.id

        if not user_id:
            return await handler(event, data)

        if user_id == ADMIN_ID:
            return await handler(event, data)
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)

        # Получаем настройки подписки из БД
        sub = await get_setting("subscription")
        if sub and sub.get("channel_id"):
            channel_id = sub["channel_id"]
            try:
                member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
                if member.status in ["left", "kicked"]:
                    button_text = sub.get("button_text", "📢 Подписаться")
                    photo_id = sub.get("photo_id")

                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [btn(button_text, url=f"https://t.me/{channel_id.replace('@', '')}", style="primary")],
                        [btn("✅ Я подписался", callback_data="check_sub", style="success")]
                    ])
                    text = "🔒 <b>Доступ закрыт!</b>\n\nЧтобы пользоваться ботом, подпишитесь на наш канал."

                    if isinstance(event, Message):
                        if photo_id:
                            await event.answer_photo(photo_id, caption=text, reply_markup=kb)
                        else:
                            await event.answer(text, reply_markup=kb)
                    else:
                        await event.message.answer(text, reply_markup=kb)
                        await event.answer()
                    return
            except Exception:
                pass  # Если бот не админ в канале, пропускаем

        return await handler(event, data)

dp.message.middleware(SubscriptionMiddleware())
dp.callback_query.middleware(SubscriptionMiddleware())

# ---------- ГЛАВНОЕ МЕНЮ ----------
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🖼 Поиск аниме", callback_data="menu_anime", style="primary")],
        [btn("🎵 Поиск музыки", callback_data="menu_music", style="success")],
        [btn("📥 Скачать видео", callback_data="menu_video", style="primary")],
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("◀️ Назад", callback_data="main_menu", style="danger")]
    ])

# ---------- /start ----------
@router.message(CommandStart())
async def start(message: Message):
    await add_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer("👋 Привет! Выбери действие:", reply_markup=main_menu_kb())

@router.callback_query(F.data == "main_menu")
async def main_menu_cb(call: CallbackQuery):
    await call.message.edit_text("Выбери действие:", reply_markup=main_menu_kb())
    await call.answer()

@router.callback_query(F.data == "check_sub")
async def check_sub_cb(call: CallbackQuery):
    await call.answer("Проверяю...", show_alert=False)
    await call.message.edit_text("Отлично! Теперь вы можете пользоваться ботом.", reply_markup=main_menu_kb())

# ---------- ПОИСК АНИМЕ ----------
@router.callback_query(F.data == "menu_anime")
async def anime_menu(call: CallbackQuery):
    await call.message.edit_text(
        "🖼 Отправь мне скриншот, видео или ссылку на изображение.",
        reply_markup=back_kb()
    )
    await call.answer()

@router.message(F.photo | F.video | F.document)
async def handle_anime_search(message: Message):
    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.video:
        file_id = message.video.file_id
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
        file_id = message.document.file_id
    else:
        await message.answer("Пожалуйста, отправь фото, видео или изображение.")
        return

    msg = await message.answer("🔍 Ищу аниме...")
    try:
        file = await bot.get_file(file_id)
        suffix = Path(file.file_path).suffix
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            await bot.download_file(file.file_path, tmp.name)
            tmp_path = tmp.name

        async with TraceMoe() as api:
            result = await api.search(tmp_path)

        os.unlink(tmp_path)

        if not result.result:
            await msg.edit_text("😔 Ничего не найдено.")
            return

        best = result.result[0]
        text = (
            f"🎬 <b>Аниме:</b> {best.filename}\n"
            f"📺 <b>Серия:</b> {best.episode if best.episode else '?'}\n"
            f"⏱ <b>Таймкод:</b> {best.from_} сек.\n"
            f"📊 <b>Схожесть:</b> {best.similarity:.1%}"
        )
        if best.video:
            await message.answer_video(best.video, caption=text)
        elif best.image:
            await message.answer_photo(best.image, caption=text)
        else:
            await message.answer(text)
        await msg.delete()

    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")

# ---------- СКАЧИВАНИЕ ВИДЕО ----------
@router.callback_query(F.data == "menu_video")
async def video_menu(call: CallbackQuery):
    await call.message.edit_text(
        "📥 Отправь ссылку на видео из TikTok, Instagram или YouTube.",
        reply_markup=back_kb()
    )
    await call.answer()

@router.message(F.text & F.text.regexp(r'https?://'))
async def handle_link(message: Message):
    url = message.text.strip()
    if re.search(r'\.(jpg|jpeg|png|gif|webp)(\?|$)', url, re.I):
        msg = await message.answer("🔍 Ищу аниме по ссылке...")
        try:
            async with TraceMoe() as api:
                result = await api.search(url, is_url=True)
            if result.result:
                best = result.result[0]
                text = (
                    f"🎬 <b>Аниме:</b> {best.filename}\n"
                    f"📺 <b>Серия:</b> {best.episode if best.episode else '?'}\n"
                    f"⏱ <b>Таймкод:</b> {best.from_} сек."
                )
                await msg.edit_text(text)
            else:
                await msg.edit_text("😔 Ничего не найдено.")
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка: {e}")
    else:
        await handle_video_link(message, url)

async def handle_video_link(message: Message, url: str):
    msg = await message.answer("⏳ Скачиваю видео...")
    tmp_dir = tempfile.mkdtemp()
    try:
        ydl_opts = {
            'outtmpl': os.path.join(tmp_dir, '%(title)s.%(ext)s'),
            'format': 'best[filesize<50M]/best',
            'quiet': True,
            'no_warnings': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)
            title = info.get('title', 'Видео')

        await message.answer_video(FSInputFile(file_path), caption=f"🎬 {title}")
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Не удалось скачать видео: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

# ---------- ПОИСК МУЗЫКИ ----------
@router.callback_query(F.data == "menu_music")
async def music_menu(call: CallbackQuery):
    await call.message.edit_text(
        "🎵 Отправь голосовое сообщение или аудиофайл.",
        reply_markup=back_kb()
    )
    await call.answer()

@router.message(F.voice | F.audio)
async def handle_music(message: Message):
    msg = await message.answer("🎧 Слушаю...")
    try:
        file_id = message.voice.file_id if message.voice else message.audio.file_id
        file = await bot.get_file(file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await bot.download_file(file.file_path, tmp.name)
            tmp_path = tmp.name

        async with Shazam() as shazam:
            out = await shazam.recognize(tmp_path)
        os.unlink(tmp_path)

        if not out.get("track"):
            await msg.edit_text("😔 Не удалось распознать песню.")
            return

        track = Serialize.full_track(out).track
        text = f"🎵 <b>{track.title}</b>\n👤 {track.subtitle}"
        if track.youtube_link:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [btn("▶️ YouTube", url=track.youtube_link, style="danger")]
            ])
            await msg.edit_text(text, reply_markup=kb)
        else:
            await msg.edit_text(text)
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")

# ---------- АДМИН-ПАНЕЛЬ ----------
@router.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("📢 Рассылка", callback_data="admin_broadcast_menu", style="primary")],
        [btn("🔒 Обязательная подписка", callback_data="admin_sub_menu", style="success")],
    ])
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=kb)
    await state.set_state(AdminStates.main_menu)

@router.callback_query(F.data == "admin_back")
async def admin_back(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.main_menu)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("📢 Рассылка", callback_data="admin_broadcast_menu", style="primary")],
        [btn("🔒 Обязательная подписка", callback_data="admin_sub_menu", style="success")],
    ])
    await call.message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=kb)
    await call.answer()

# ---------- УПРАВЛЕНИЕ ПОДПИСКОЙ ----------
@router.callback_query(F.data == "admin_sub_menu")
async def admin_sub_menu(call: CallbackQuery, state: FSMContext):
    sub = await get_setting("subscription")
    status = "✅ Настроена" if sub and sub.get("channel_id") else "❌ Не настроена"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("✏️ Настроить", callback_data="sub_setup", style="primary")],
        [btn("🗑 Удалить", callback_data="sub_delete", style="danger")],
        [btn("◀️ Назад", callback_data="admin_back", style="danger")],
    ])
    await call.message.edit_text(f"🔒 <b>Обязательная подписка</b>\nСтатус: {status}", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "sub_setup")
async def sub_setup_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("Введи <b>название кнопки</b> (например, \"📢 Подписаться\"):")
    await state.set_state(AdminStates.sub_button_name)
    await call.answer()

@router.message(AdminStates.sub_button_name)
async def sub_button_name(message: Message, state: FSMContext):
    await state.update_data(sub_button_name=message.text)
    await message.answer("Теперь скинь <b>ссылку на канал</b> (например, @my_channel или https://t.me/my_channel):")
    await state.set_state(AdminStates.sub_channel_link)

@router.message(AdminStates.sub_channel_link)
async def sub_channel_link(message: Message, state: FSMContext):
    link = message.text.strip()
    if "t.me/" in link:
        channel_id = "@" + link.split("t.me/")[-1].split("/")[0]
    else:
        channel_id = link
    await state.update_data(sub_channel_id=channel_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("⏭ Пропустить", callback_data="sub_skip_photo", style="danger")]
    ])
    await message.answer("Теперь можешь отправить <b>фото</b> для красоты, или нажми \"Пропустить\".", reply_markup=kb)
    await state.set_state(AdminStates.sub_photo)

@router.message(AdminStates.sub_photo, F.photo)
async def sub_photo(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    data = await state.get_data()
    new_sub = {
        "button_text": data["sub_button_name"],
        "channel_id": data["sub_channel_id"],
        "photo_id": photo_id
    }
    await set_setting("subscription", new_sub)
    await message.answer("✅ Подписка успешно настроена!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [btn("◀️ В админку", callback_data="admin_back", style="danger")]
    ]))
    await state.set_state(AdminStates.main_menu)

@router.callback_query(F.data == "sub_skip_photo")
async def sub_skip_photo(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    new_sub = {
        "button_text": data["sub_button_name"],
        "channel_id": data["sub_channel_id"],
        "photo_id": None
    }
    await set_setting("subscription", new_sub)
    await call.message.edit_text("✅ Подписка настроена (без фото).", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [btn("◀️ В админку", callback_data="admin_back", style="danger")]
    ]))
    await state.set_state(AdminStates.main_menu)
    await call.answer()

@router.callback_query(F.data == "sub_delete")
async def sub_delete(call: CallbackQuery):
    await set_setting("subscription", {})
    await call.answer("Удалено!", show_alert=True)
    await admin_sub_menu(call, None)

# ---------- РАССЫЛКА ----------
@router.callback_query(F.data == "admin_broadcast_menu")
async def broadcast_menu(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("✅ С кнопкой", callback_data="broadcast_with_btn", style="success")],
        [btn("❌ Без кнопки", callback_data="broadcast_without_btn", style="danger")],
        [btn("◀️ Назад", callback_data="admin_back", style="danger")],
    ])
    await call.message.edit_text(
        "📢 <b>Рассылка</b>\n\n"
        "Выбери тип:\n"
        "• <b>С кнопкой</b> — сообщение с кнопкой-ссылкой.\n"
        "• <b>Без кнопки</b> — просто текст, гиф, стикер или медиа.",
        reply_markup=kb
    )
    await call.answer()

# --- С КНОПКОЙ ---
@router.callback_query(F.data == "broadcast_with_btn")
async def broadcast_with_btn(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("Отправь <b>текст</b> для рассылки (с кнопкой):")
    await state.set_state(AdminStates.broadcast_text)
    await call.answer()

@router.message(AdminStates.broadcast_text)
async def broadcast_text_with_btn(message: Message, state: FSMContext):
    await state.update_data(broadcast_text=message.text)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("⏭ Пропустить фото", callback_data="broadcast_skip_photo_btn", style="danger")]
    ])
    await message.answer("Добавь <b>фото</b> или нажми \"Пропустить\".", reply_markup=kb)
    await state.set_state(AdminStates.broadcast_photo)

@router.message(AdminStates.broadcast_photo, F.photo)
async def broadcast_photo_with_btn(message: Message, state: FSMContext):
    await state.update_data(broadcast_photo=message.photo[-1].file_id)
    await show_broadcast_preview(message, state)

@router.callback_query(F.data == "broadcast_skip_photo_btn")
async def broadcast_skip_photo_btn(call: CallbackQuery, state: FSMContext):
    await state.update_data(broadcast_photo=None)
    await show_broadcast_preview(call.message, state)
    await call.answer()

# --- БЕЗ КНОПКИ ---
@router.callback_query(F.data == "broadcast_without_btn")
async def broadcast_without_btn(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("Отправь <b>что угодно</b> для рассылки (текст, гиф, стикер, аудио):")
    await state.set_state(AdminStates.broadcast_preview)
    await call.answer()

@router.message(AdminStates.broadcast_preview)
async def broadcast_without_btn_preview(message: Message, state: FSMContext):
    await state.update_data(copy_message_id=message.message_id, copy_from_chat=message.chat.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("🚀 Отправить", callback_data="broadcast_confirm", style="success")],
        [btn("◀️ Назад", callback_data="admin_back", style="danger")],
    ])
    await message.answer("👀 <b>Предпросмотр:</b>")
    await message.copy_to(chat_id=message.chat.id)
    await message.answer("Подтверди отправку:", reply_markup=kb)

# --- ПРЕДПРОСМОТР (ДЛЯ ВАРИАНТА С КНОПКОЙ) ---
async def show_broadcast_preview(message: Message, state: FSMContext):
    data = await state.get_data()
    text = data.get("broadcast_text", "")
    photo_id = data.get("broadcast_photo")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("🚀 Отправить", callback_data="broadcast_confirm", style="success")],
        [btn("◀️ Назад", callback_data="admin_back", style="danger")],
    ])

    await message.answer("👀 <b>Предпросмотр:</b>")
    if photo_id:
        await message.answer_photo(photo_id, caption=text)
    else:
        await message.answer(text)

    await message.answer("Подтверди отправку:", reply_markup=kb)
    await state.set_state(AdminStates.broadcast_preview)

# --- ПОДТВЕРЖДЕНИЕ И ОТПРАВКА ---
@router.callback_query(F.data == "broadcast_confirm")
async def broadcast_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_ids = await get_all_users()
    if not user_ids:
        await call.message.edit_text("❌ Нет пользователей для рассылки.")
        await state.clear()
        return

    await call.message.edit_text(f"📤 Начинаю рассылку на {len(user_ids)} чел...")

    reply_markup = None
    if "broadcast_text" in data and "broadcast_photo" in data:
        reply_markup = InlineKeyboardMarkup(inline_keyboard=[
            [btn("🔗 Перейти", url="https://t.me/durov", style="primary")]  # ЗАМЕНИ ССЫЛКУ
        ])

    success = 0
    fail = 0
    for uid in user_ids:
        try:
            if "copy_message_id" in data:
                await bot.copy_message(
                    chat_id=uid,
                    from_chat_id=data["copy_from_chat"],
                    message_id=data["copy_message_id"]
                )
            else:
                if data.get("broadcast_photo"):
                    await bot.send_photo(uid, data["broadcast_photo"], caption=data["broadcast_text"], reply_markup=reply_markup)
                else:
                    await bot.send_message(uid, data["broadcast_text"], reply_markup=reply_markup)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1

    await call.message.edit_text(f"✅ Рассылка завершена.\nУспешно: {success}\nОшибок: {fail}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [btn("◀️ В админку", callback_data="admin_back", style="danger")]
    ]))
    await state.clear()

# ---------- ЗАПУСК ----------
async def main():
    global db_pool
    print("Подключение к базе данных...")
    # Создаем пул соединений. Neon рекомендует использовать pooler для Render.
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    await init_db()
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
