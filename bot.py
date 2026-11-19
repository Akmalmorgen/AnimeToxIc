import asyncio
import json
import os
import re
import tempfile
import shutil
import time
from pathlib import Path

import asyncpg
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from shazamio import Shazam, Serialize
import yt_dlp

# ---------- КОНФИГУРАЦИЯ ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
TRACEMOE_TOKEN = os.getenv("TRACEMOE_TOKEN")
BOT_USERNAME = "Animebot1k_bot" # <-- Укажи юзернейм бота для подписи

# ---------- АНТИСПАМ (ХРАНИЛИЩЕ В ПАМЯТИ) ----------
# Ключ: user_id, Значение: время последнего запроса
user_last_request = {}
SPAM_DELAY = 3  # Секунды

async def check_spam(user_id: int) -> bool:
    """Возвращает True, если пользователь спамит."""
    now = time.time()
    last_time = user_last_request.get(user_id, 0)
    if now - last_time < SPAM_DELAY:
        return True
    user_last_request[user_id] = now
    return False

# ---------- АНИМАЦИЯ ЗАГРУЗКИ ----------
async def loading_animation(message: Message, base_text: str):
    """Асинхронная анимация ползунка."""
    bars = ["░", "▒", "▓", "█"]
    try:
        for i in range(1, 11): # 10 шагов
            filled = bars[min(i // 3, 3)] * i
            empty = "░" * (10 - i)
            await message.edit_text(f"{base_text}\n\n[{filled}{empty}] {i*10}%")
            await asyncio.sleep(0.15) # Скорость анимации
    except Exception:
        pass # Игнорируем ошибки, если сообщение удалили

# ---------- ИНИЦИАЛИЗАЦИЯ БОТА ----------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ---------- ПУЛ СОЕДИНЕНИЙ С БД ----------
db_pool: asyncpg.Pool = None

async def init_db():
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value JSONB
            );
        """)
    print("✅ База данных инициализирована.")

async def get_setting(key: str):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = $1", key)
        return row["value"] if row else None

async def set_setting(key: str, value):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO settings (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = $2
        """, key, json.dumps(value))

async def add_user(user_id: int, username: str = None, first_name: str = None):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username, first_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO NOTHING
        """, user_id, username, first_name)

async def get_all_users():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM users")
        return [row["user_id"] for row in rows]

# ---------- СОСТОЯНИЯ (FSM) ----------
class AdminStates(StatesGroup):
    main_menu = State()
    sub_button_name = State()
    sub_channel_link = State()
    sub_photo = State()
    broadcast_text = State()
    broadcast_photo = State()
    broadcast_preview = State()

class UserStates(StatesGroup):
    waiting_music = State()

# ---------- ХЕЛПЕР ДЛЯ INLINE-КНОПОК ----------
def btn(text, callback_data=None, url=None, style=None):
    return InlineKeyboardButton(text=text, callback_data=callback_data, url=url, style=style)

# ---------- MIDDLEWARE: ОБЯЗАТЕЛЬНАЯ ПОДПИСКА ----------
class SubscriptionMiddleware:
    async def __call__(self, handler, event, data):
        user_id = None
        if isinstance(event, Message): user_id = event.from_user.id
        elif isinstance(event, CallbackQuery): user_id = event.from_user.id

        if not user_id: return await handler(event, data)
        if user_id == ADMIN_ID: return await handler(event, data)
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)

        # Проверка на спам
        if await check_spam(user_id):
            if isinstance(event, Message):
                await event.answer(f"⏳ Пожалуйста, не так быстро. Подожди {SPAM_DELAY} секунды.")
            else:
                await event.answer("⏳ Слишком быстро! Подожди немного.", show_alert=True)
            return

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
                        if photo_id: await event.answer_photo(photo_id, caption=text, reply_markup=kb)
                        else: await event.answer(text, reply_markup=kb)
                    else:
                        await event.message.answer(text, reply_markup=kb)
                        await event.answer()
                    return
            except Exception: pass
        return await handler(event, data)

dp.message.middleware(SubscriptionMiddleware())
dp.callback_query.middleware(SubscriptionMiddleware())

# ---------- КЛАВИАТУРЫ (REPLY И INLINE) ----------
def main_menu_reply(user_id):
    kb = [
        [KeyboardButton(text="🖼 Поиск аниме"), KeyboardButton(text="🎵 Поиск музыки")],
        [KeyboardButton(text="📥 Скачать видео")]
    ]
    if user_id == ADMIN_ID:
        kb.append([KeyboardButton(text="🛠 Админка")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def back_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[btn("◀️ Назад в меню", callback_data="main_menu", style="danger")]])

def cancel_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Отмена", callback_data="main_menu", style="danger")]])

def admin_cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Отмена", callback_data="admin_back", style="danger")]])

# ---------- СТАРТ И ГЛАВНОЕ МЕНЮ ----------
@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await add_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(
        "👋 <b>Привет!</b> Я — твой помощник в мире аниме и развлечений.\n\n"
        "✨ <b>Что я умею:</b>\n"
        "• Находить аниме по скриншоту\n"
        "• Искать и распознавать музыку\n"
        "• Скачивать видео из соцсетей\n\n"
        "👇 Выбери нужный раздел на клавиатуре ниже:",
        reply_markup=main_menu_reply(message.from_user.id)
    )

@router.callback_query(F.data == "main_menu")
async def main_menu_handler(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()
    await call.message.answer("🏠 <b>Главное меню</b>\n\nВыбери действие:", reply_markup=main_menu_reply(call.from_user.id))
    await call.answer()

@router.callback_query(F.data == "check_sub")
async def check_sub_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("Проверяю...", show_alert=False)
    await call.message.edit_text("✅ Отлично! Доступ открыт.")
    await call.message.answer("🏠 <b>Главное меню</b>", reply_markup=main_menu_reply(call.from_user.id))

# ---------- ПОИСК АНИМЕ ----------
async def search_tracemoe_api(image_bytes, is_url=False):
    async with aiohttp.ClientSession() as session:
        url = "https://api.trace.moe/search"
        headers = {}
        if TRACEMOE_TOKEN: headers["x-trace-token"] = TRACEMOE_TOKEN
        try:
            if is_url:
                async with session.get(url, params={"url": image_bytes}, headers=headers, timeout=15) as resp:
                    return await resp.json()
            else:
                form = aiohttp.FormData()
                form.add_field('file', image_bytes, filename='image.jpg')
                async with session.post(url, data=form, headers=headers, timeout=15) as resp:
                    return await resp.json()
        except asyncio.TimeoutError: return {"error": "timeout"}
        except Exception as e: return {"error": str(e)}

@router.message(F.text == "🖼 Поиск аниме")
async def anime_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🖼 <b>Поиск аниме по кадру</b>\n\n"
        "📌 <b>Что нужно:</b> Отправь мне <b>полный и необрезанный</b> скриншот из аниме.\n\n"
        "⚠️ <b>Важно:</b> Я ищу только реальные кадры из аниме. Фанарты, AI-арт и сильно обрезанные картинки я найти не смогу.",
        reply_markup=back_inline_kb()
    )

@router.message(F.photo | F.video | F.document)
async def handle_anime_search(message: Message, state: FSMContext):
    if await state.get_state() == UserStates.waiting_music:
        return await message.answer("🎵 Ты сейчас в режиме поиска музыки. Нажми «Отмена» или отправь аудио/текст.")

    file_id = None
    if message.photo: file_id = message.photo[-1].file_id
    elif message.video: file_id = message.video.file_id
    elif message.document and message.document.mime_type.startswith("image/"): file_id = message.document.file_id
    else: return

    msg = await message.answer("🔍 <b>Анализирую кадр...</b>")
    try:
        file = await bot.get_file(file_id)
        file_data = await bot.download_file(file.file_path)
        result = await search_tracemoe_api(file_data.read())

        if result.get("error"): return await msg.edit_text(f"❌ Ошибка: {result['error']}")
        if not result.get("result"):
            return await msg.edit_text("😔 <b>Ничего не найдено.</b>\n\nВозможно, это фанарт, AI-арт или кадр сильно обрезан.")

        best = result["result"][0]
        text = (
            f"🎬 <b>Аниме:</b> {best.get('filename', 'Неизвестно')}\n"
            f"📺 <b>Серия:</b> {best.get('episode', '?')}\n"
            f"⏱ <b>Таймкод:</b> {int(best.get('from', 0))} сек.\n"
            f"📊 <b>Схожесть:</b> {best.get('similarity', 0) * 100:.1f}%"
        )
        if best.get("video"): await message.answer_video(best["video"], caption=text)
        elif best.get("image"): await message.answer_photo(best["image"], caption=text)
        else: await message.answer(text)
        await msg.delete()
    except Exception as e: await msg.edit_text(f"❌ Ошибка: {e}")

# ---------- ССЫЛКИ (АНИМЕ) ----------
@router.message(F.text & F.text.regexp(r'https?://'))
async def handle_link(message: Message, state: FSMContext):
    if await state.get_state() == UserStates.waiting_music: return
    url = message.text.strip()
    if re.search(r'\.(jpg|jpeg|png|gif|webp)(\?|$)', url, re.I):
        msg = await message.answer("🔍 <b>Ищу аниме по ссылке...</b>")
        result = await search_tracemoe_api(url, is_url=True)
        if result.get("error"): await msg.edit_text(f"❌ Ошибка: {result['error']}")
        elif not result.get("result"): await msg.edit_text("😔 Ничего не найдено.")
        else:
            best = result["result"][0]
            await msg.edit_text(f"🎬 <b>Аниме:</b> {best.get('filename')}\n📺 <b>Серия:</b> {best.get('episode')}\n⏱ <b>Таймкод:</b> {int(best.get('from', 0))} сек.")
    else:
        await handle_video_link(message, url)

# ---------- СКАЧИВАНИЕ ВИДЕО ----------
@router.message(F.text == "📥 Скачать видео")
async def video_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "📥 <b>Скачивание видео</b>\n\n"
        "📌 <b>Что нужно:</b> Просто отправь мне ссылку на видео из <b>YouTube, TikTok, Instagram</b> или другой соцсети.\n\n"
        "✨ <b>Что я сделаю:</b> Скачаю видео и отправлю его тебе прямо в чат.",
        reply_markup=back_inline_kb()
    )

async def handle_video_link(message: Message, url: str):
    msg = await message.answer("⏳ <b>Начинаю загрузку...</b>")
    # Запускаем анимацию в фоне
    anim_task = asyncio.create_task(loading_animation(msg, "⏳ <b>Скачиваю видео...</b>"))
    
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

        anim_task.cancel() # Останавливаем анимацию
        await msg.edit_text("✅ <b>Видео готово!</b> Отправляю...")
        await message.answer_video(FSInputFile(file_path), caption=f"🎬 <b>{title}</b>\n\n📥 Скачано через @{BOT_USERNAME}")
        await msg.delete()
    except Exception as e:
        anim_task.cancel()
        await msg.edit_text(f"❌ Не удалось скачать видео: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

# ---------- ПОИСК МУЗЫКИ ----------
@router.message(F.text == "🎵 Поиск музыки")
async def music_menu(message: Message, state: FSMContext):
    await state.set_state(UserStates.waiting_music)
    await message.answer(
        "🎵 <b>Поиск музыки</b>\n\n"
        "📌 <b>Что можно отправить:</b>\n"
        "• 🎤 Голосовое сообщение или аудиофайл (распознаю через Shazam)\n"
        "• 📝 Текст с названием песни (найду на YouTube)\n"
        "• 🔗 Ссылку на видео (вытащу звук и распознаю)\n"
        "• 🎬 Видеофайл (вытащу звук и распознаю)\n\n"
        "Выбери любой удобный способ!",
        reply_markup=cancel_inline_kb()
    )

@router.message(UserStates.waiting_music, F.text)
async def handle_music_text(message: Message, state: FSMContext):
    if message.text.startswith("http"): return await handle_music_link(message, state)
    msg = await message.answer("🔎 <b>Ищу на YouTube...</b>")
    try:
        with yt_dlp.YoutubeDL({'default_search': 'ytsearch1', 'quiet': True, 'extract_flat': True}) as ydl:
            info = ydl.extract_info(f"ytsearch1:{message.text}", download=False)
            if info.get('entries'):
                video = info['entries'][0]
                await msg.edit_text(f"🎵 <b>Найдено:</b>\n{video.get('title')}\n\n🔗 <a href='{video.get('url')}'>Ссылка на YouTube</a>", reply_markup=back_inline_kb())
            else:
                await msg.edit_text("😔 Ничего не найдено.", reply_markup=back_inline_kb())
    except Exception as e: await msg.edit_text(f"❌ Ошибка: {e}", reply_markup=back_inline_kb())

async def handle_music_link(message: Message, state: FSMContext):
    msg = await message.answer("🎧 <b>Анализирую ссылку...</b>")
    anim_task = asyncio.create_task(loading_animation(msg, "🎧 <b>Скачиваю и слушаю...</b>"))
    tmp_dir = tempfile.mkdtemp()
    try:
        with yt_dlp.YoutubeDL({'format': 'bestaudio/best', 'outtmpl': os.path.join(tmp_dir, 'audio.%(ext)s'), 'quiet': True, 'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}]}) as ydl:
            ydl.extract_info(message.text.strip(), download=True)
            file_path = os.path.join(tmp_dir, 'audio.mp3')
        async with Shazam() as shazam:
            out = await shazam.recognize(file_path)
        anim_task.cancel()
        if not out.get("track"): return await msg.edit_text("😔 Не удалось распознать песню.", reply_markup=back_inline_kb())
        track = Serialize.full_track(out).track
        text = f"🎵 <b>{track.title}</b>\n👤 {track.subtitle}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("▶️ YouTube", url=track.youtube_link, style="danger")], [btn("◀️ Назад в меню", callback_data="main_menu", style="danger")]]) if track.youtube_link else back_inline_kb()
        await msg.edit_text(text, reply_markup=kb)
    except Exception as e:
        anim_task.cancel()
        await msg.edit_text(f"❌ Ошибка: {e}", reply_markup=back_inline_kb())
    finally: shutil.rmtree(tmp_dir, ignore_errors=True)

@router.message(UserStates.waiting_music, F.voice | F.audio | F.video)
async def handle_music_media(message: Message, state: FSMContext):
    msg = await message.answer("🎧 <b>Слушаю...</b>")
    anim_task = asyncio.create_task(loading_animation(msg, "🎧 <b>Анализирую звук...</b>"))
    tmp_dir = tempfile.mkdtemp()
    try:
        file_id = message.voice.file_id if message.voice else (message.audio.file_id if message.audio else message.video.file_id)
        ext = ".ogg" if message.voice else (".mp3" if message.audio else ".mp4")
        file = await bot.get_file(file_id)
        input_path = os.path.join(tmp_dir, f"input{ext}")
        await bot.download_file(file.file_path, input_path)

        process_path = input_path
        if message.video:
            audio_path = os.path.join(tmp_dir, "audio.mp3")
            os.system(f"ffmpeg -i {input_path} -q:a 0 -map a {audio_path} -y -loglevel quiet")
            process_path = audio_path

        async with Shazam() as shazam:
            out = await shazam.recognize(process_path)

        anim_task.cancel()
        if not out.get("track"): return await msg.edit_text("😔 Не удалось распознать песню.", reply_markup=back_inline_kb())
        track = Serialize.full_track(out).track
        text = f"🎵 <b>{track.title}</b>\n👤 {track.subtitle}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("▶️ YouTube", url=track.youtube_link, style="danger")], [btn("◀️ Назад в меню", callback_data="main_menu", style="danger")]]) if track.youtube_link else back_inline_kb()
        await msg.edit_text(text, reply_markup=kb)
    except Exception as e:
        anim_task.cancel()
        await msg.edit_text(f"❌ Ошибка: {e}", reply_markup=back_inline_kb())
    finally: shutil.rmtree(tmp_dir, ignore_errors=True)

# ---------- АДМИН-ПАНЕЛЬ ----------
@router.message(F.text == "🛠 Админка")
@router.message(Command("admin"))
async def admin_panel_cmd(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await show_admin_panel(message, state)

@router.callback_query(F.data == "admin_back")
async def admin_back(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
    await show_admin_panel(call.message, state)
    await call.answer()

async def show_admin_panel(message: Message, state: FSMContext):
    await state.set_state(AdminStates.main_menu)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("📢 Рассылка", callback_data="admin_broadcast_menu", style="primary")],
        [btn("🔒 Обязательная подписка", callback_data="admin_sub_menu", style="success")],
        [btn("◀️ В меню", callback_data="main_menu", style="danger")],
    ])
    try: await message.edit_text("🛠 <b>Админ-панель</b>\n\nВыбери раздел:", reply_markup=kb)
    except: await message.answer("🛠 <b>Админ-панель</b>\n\nВыбери раздел:", reply_markup=kb)

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
    await call.message.edit_text(f"🔒 <b>Обязательная подписка</b>\n\nСтатус: {status}\n\nЗдесь ты можешь настроить канал, на который должны быть подписаны пользователи.", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "sub_setup")
async def sub_setup_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("📝 <b>Шаг 1/3:</b> Введи <b>название кнопки</b> (например, \"📢 Подписаться\"):", reply_markup=admin_cancel_kb())
    await state.set_state(AdminStates.sub_button_name)
    await call.answer()

@router.message(AdminStates.sub_button_name)
async def sub_button_name(message: Message, state: FSMContext):
    await state.update_data(sub_button_name=message.text)
    await message.answer("📝 <b>Шаг 2/3:</b> Скинь <b>ссылку на канал</b> (например, @my_channel или https://t.me/my_channel):", reply_markup=admin_cancel_kb())
    await state.set_state(AdminStates.sub_channel_link)

@router.message(AdminStates.sub_channel_link)
async def sub_channel_link(message: Message, state: FSMContext):
    link = message.text.strip()
    channel_id = "@" + link.split("t.me/")[-1].split("/")[0] if "t.me/" in link else link
    await state.update_data(sub_channel_id=channel_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("⏭ Пропустить", callback_data="sub_skip_photo", style="success")], [btn("❌ Отмена", callback_data="admin_back", style="danger")]])
    await message.answer("📝 <b>Шаг 3/3:</b> Отправь <b>фото</b> для красоты, или нажми \"Пропустить\".", reply_markup=kb)
    await state.set_state(AdminStates.sub_photo)

@router.message(AdminStates.sub_photo, F.photo)
async def sub_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    await set_setting("subscription", {"button_text": data["sub_button_name"], "channel_id": data["sub_channel_id"], "photo_id": message.photo[-1].file_id})
    await message.answer("✅ <b>Подписка успешно настроена!</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("◀️ В админку", callback_data="admin_back", style="danger")]]))
    await state.set_state(AdminStates.main_menu)

@router.callback_query(F.data == "sub_skip_photo")
async def sub_skip_photo(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await set_setting("subscription", {"button_text": data["sub_button_name"], "channel_id": data["sub_channel_id"], "photo_id": None})
    await call.message.edit_text("✅ <b>Подписка настроена (без фото).</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("◀️ В админку", callback_data="admin_back", style="danger")]]))
    await state.set_state(AdminStates.main_menu)
    await call.answer()

@router.callback_query(F.data == "sub_delete")
async def sub_delete(call: CallbackQuery, state: FSMContext):
    await set_setting("subscription", {})
    await call.answer("🗑 Удалено!", show_alert=True)
    await admin_sub_menu(call, state)

# ---------- РАССЫЛКА ----------
@router.callback_query(F.data == "admin_broadcast_menu")
async def broadcast_menu(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("✅ С кнопкой", callback_data="broadcast_with_btn", style="success")],
        [btn("❌ Без кнопки", callback_data="broadcast_without_btn", style="danger")],
        [btn("◀️ Назад", callback_data="admin_back", style="danger")],
    ])
    await call.message.edit_text("📢 <b>Рассылка</b>\n\nВыбери тип сообщения:", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "broadcast_with_btn")
async def broadcast_with_btn(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("✍️ Отправь <b>текст</b> для рассылки (с кнопкой):", reply_markup=admin_cancel_kb())
    await state.set_state(AdminStates.broadcast_text)
    await call.answer()

@router.message(AdminStates.broadcast_text)
async def broadcast_text_with_btn(message: Message, state: FSMContext):
    await state.update_data(broadcast_text=message.text)
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("⏭ Пропустить фото", callback_data="broadcast_skip_photo_btn", style="success")], [btn("❌ Отмена", callback_data="admin_back", style="danger")]])
    await message.answer("🖼️ Добавь <b>фото</b> или нажми \"Пропустить\".", reply_markup=kb)
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

@router.callback_query(F.data == "broadcast_without_btn")
async def broadcast_without_btn(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("✍️ Отправь <b>что угодно</b> для рассылки (текст, фото, видео, стикер):", reply_markup=admin_cancel_kb())
    await state.set_state(AdminStates.broadcast_preview)
    await call.answer()

@router.message(AdminStates.broadcast_preview)
async def broadcast_without_btn_preview(message: Message, state: FSMContext):
    await state.update_data(copy_message_id=message.message_id, copy_from_chat=message.chat.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("🚀 Отправить", callback_data="broadcast_confirm", style="success")], [btn("◀️ Назад", callback_data="admin_back", style="danger")]])
    await message.answer("👀 <b>Предпросмотр:</b>")
    await message.copy_to(chat_id=message.chat.id)
    await message.answer("Подтверди отправку:", reply_markup=kb)

async def show_broadcast_preview(message: Message, state: FSMContext):
    data = await state.get_data()
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("🚀 Отправить", callback_data="broadcast_confirm", style="success")], [btn("◀️ Назад", callback_data="admin_back", style="danger")]])
    await message.answer("👀 <b>Предпросмотр:</b>")
    if data.get("broadcast_photo"): await message.answer_photo(data["broadcast_photo"], caption=data.get("broadcast_text", ""))
    else: await message.answer(data.get("broadcast_text", ""))
    await message.answer("Подтверди отправку:", reply_markup=kb)
    await state.set_state(AdminStates.broadcast_preview)

@router.callback_query(F.data == "broadcast_confirm")
async def broadcast_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_ids = await get_all_users()
    if not user_ids:
        await call.message.edit_text("❌ Нет пользователей для рассылки.")
        return await state.clear()

    await call.message.edit_text(f"📤 Рассылка на {len(user_ids)} чел...")
    reply_markup = InlineKeyboardMarkup(inline_keyboard=[[btn("🔗 Перейти", url="https://t.me/durov", style="primary")]]) if "broadcast_text" in data else None
    success, fail = 0, 0
    for uid in user_ids:
        try:
            if "copy_message_id" in data: await bot.copy_message(chat_id=uid, from_chat_id=data["copy_from_chat"], message_id=data["copy_message_id"])
            elif data.get("broadcast_photo"): await bot.send_photo(uid, data["broadcast_photo"], caption=data["broadcast_text"], reply_markup=reply_markup)
            else: await bot.send_message(uid, data["broadcast_text"], reply_markup=reply_markup)
            success += 1
            await asyncio.sleep(0.05)
        except Exception: fail += 1

    await call.message.edit_text(f"✅ <b>Рассылка завершена.</b>\n\nУспешно: {success}\nОшибок: {fail}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("◀️ В админку", callback_data="admin_back", style="danger")]]))
    await state.clear()

# ---------- HEALTH CHECK ----------
async def health(request): return web.Response(text="OK", status=200)

async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000)))
    await site.start()
    print(f"✅ Health-check сервер слушает порт {os.environ.get('PORT', 10000)}")

# ---------- ЗАПУСК ----------
async def main():
    global db_pool
    print("Подключение к базе данных...")
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    await init_db()
    asyncio.create_task(start_health_server())
    print("Бот запущен (long polling)...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
