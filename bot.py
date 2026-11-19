import asyncio
import json
import os
import re
import tempfile
import shutil
import base64
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
    FSInputFile
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

# ---------- ХЕЛПЕР ДЛЯ КНОПОК ----------
def btn(text, callback_data=None, url=None, style=None):
    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        url=url,
        style=style 
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
                pass

        return await handler(event, data)

dp.message.middleware(SubscriptionMiddleware())
dp.callback_query.middleware(SubscriptionMiddleware())

# ---------- КЛАВИАТУРЫ ----------
def main_menu_kb(user_id):
    kb = [
        [btn("🖼 Поиск аниме", callback_data="menu_anime", style="primary")],
        [btn("🎵 Поиск музыки", callback_data="menu_music", style="success")],
        [btn("📥 Скачать видео", callback_data="menu_video", style="primary")],
    ]
    if user_id == ADMIN_ID:
        kb.append([btn("🛠 Админка", callback_data="admin_back", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("◀️ Назад в меню", callback_data="main_menu", style="danger")]
    ])

def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("❌ Отмена", callback_data="main_menu", style="danger")]
    ])

def admin_cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("❌ Отмена", callback_data="admin_back", style="danger")]
    ])

# ---------- СТАРТ ----------
@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await add_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer("👋 Привет! Выбери действие:", reply_markup=main_menu_kb(message.from_user.id))

@router.callback_query(F.data == "main_menu")
async def main_menu_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Выбери действие:", reply_markup=main_menu_kb(call.from_user.id))
    await call.answer()

@router.callback_query(F.data == "check_sub")
async def check_sub_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("Проверяю...", show_alert=False)
    await call.message.edit_text("Отлично! Теперь вы можете пользоваться ботом.", reply_markup=main_menu_kb(call.from_user.id))

# ---------- ПОИСК АНИМЕ ----------
async def search_tracemoe_api(image_bytes, is_url=False):
    async with aiohttp.ClientSession() as session:
        url = "https://api.trace.moe/search"
        headers = {}
        if TRACEMOE_TOKEN:
            headers["x-trace-token"] = TRACEMOE_TOKEN

        try:
            if is_url:
                params = {"url": image_bytes}
                async with session.get(url, params=params, headers=headers, timeout=15) as resp:
                    return await resp.json()
            else:
                form = aiohttp.FormData()
                form.add_field('file', image_bytes, filename='image.jpg')
                async with session.post(url, data=form, headers=headers, timeout=15) as resp:
                    return await resp.json()
        except asyncio.TimeoutError:
            return {"error": "timeout"}
        except Exception as e:
            return {"error": str(e)}

@router.callback_query(F.data == "menu_anime")
async def anime_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(
        "🖼 Отправь мне скриншот, видео или ссылку на изображение из аниме.",
        reply_markup=back_kb()
    )
    await call.answer()

@router.message(F.photo | F.video | F.document)
async def handle_anime_search(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state == UserStates.waiting_music:
        await message.answer("Ты сейчас в режиме поиска музыки. Сначала нажми «Отмена» или скинь аудио/текст.")
        return

    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.video:
        file_id = message.video.file_id
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
        file_id = message.document.file_id
    else:
        return

    msg = await message.answer("🔍 Ищу аниме...")
    try:
        file = await bot.get_file(file_id)
        file_data = await bot.download_file(file.file_path)
        
        result = await search_tracemoe_api(file_data.read())

        if result.get("error"):
            await msg.edit_text(f"❌ Ошибка поиска: {result['error']}")
            return

        if not result.get("result"):
            await msg.edit_text("😔 Ничего не найдено. Скорее всего, это не кадр из аниме (например, фанарт или AI-арт).")
            return

        best = result["result"][0]
        similarity = best.get("similarity", 0) * 100
        episode = best.get("episode", "?")
        time_from = int(best.get("from", 0))
        
        text = (
            f"🎬 <b>Аниме:</b> {best.get('filename', 'Неизвестно')}\n"
            f"📺 <b>Серия:</b> {episode}\n"
            f"⏱ <b>Таймкод:</b> {time_from} сек.\n"
            f"📊 <b>Схожесть:</b> {similarity:.1f}%"
        )
        
        if best.get("video"):
            await message.answer_video(best["video"], caption=text)
        elif best.get("image"):
            await message.answer_photo(best["image"], caption=text)
        else:
            await message.answer(text)
            
        await msg.delete()

    except Exception as e:
        await msg.edit_text(f"❌ Системная ошибка: {e}")

# ---------- ССЫЛКИ (АНИМЕ) ----------
@router.message(F.text & F.text.regexp(r'https?://'))
async def handle_link(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state == UserStates.waiting_music:
        return

    url = message.text.strip()
    if re.search(r'\.(jpg|jpeg|png|gif|webp)(\?|$)', url, re.I):
        msg = await message.answer("🔍 Ищу аниме по ссылке...")
        result = await search_tracemoe_api(url, is_url=True)
        
        if result.get("error"):
            await msg.edit_text(f"❌ Ошибка: {result['error']}")
        elif not result.get("result"):
            await msg.edit_text("😔 Ничего не найдено.")
        else:
            best = result["result"][0]
            text = (
                f"🎬 <b>Аниме:</b> {best.get('filename', 'Неизвестно')}\n"
                f"📺 <b>Серия:</b> {best.get('episode', '?')}\n"
                f"⏱ <b>Таймкод:</b> {int(best.get('from', 0))} сек."
            )
            await msg.edit_text(text)
    else:
        await handle_video_link(message, url)

# ---------- СКАЧИВАНИЕ ВИДЕО ----------
@router.callback_query(F.data == "menu_video")
async def video_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(
        "📥 Отправь ссылку на видео из TikTok, Instagram или YouTube.",
        reply_markup=back_kb()
    )
    await call.answer()

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
async def music_menu(call: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_music)
    await call.message.edit_text(
        "🎵 <b>Поиск музыки</b>\n\n"
        "Отправь мне:\n"
        "• Голосовое или аудио (найду через Shazam)\n"
        "• Текст (название песни, найду на YouTube)\n"
        "• Ссылку на видео (вытащу звук и найду)\n"
        "• Видеофайл (вытащу звук и найду)",
        reply_markup=cancel_kb()
    )
    await call.answer()

@router.message(UserStates.waiting_music, F.text)
async def handle_music_text(message: Message, state: FSMContext):
    query = message.text
    if query.startswith("http"):
        return await handle_music_link(message, state)
    
    msg = await message.answer("🔎 Ищу на YouTube...")
    try:
        ydl_opts = {'default_search': 'ytsearch1', 'quiet': True, 'extract_flat': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
            if 'entries' in info and info['entries']:
                video = info['entries'][0]
                title = video.get('title')
                url = video.get('url')
                await msg.edit_text(f"🎵 <b>Найдено по запросу:</b>\n{title}\n\n🔗 <a href='{url}'>Ссылка на YouTube</a>", reply_markup=back_kb())
            else:
                await msg.edit_text("😔 Ничего не найдено по тексту.", reply_markup=back_kb())
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка поиска: {e}", reply_markup=back_kb())

async def handle_music_link(message: Message, state: FSMContext):
    url = message.text.strip()
    msg = await message.answer("🎧 Скачиваю аудио из ссылки и слушаю...")
    tmp_dir = tempfile.mkdtemp()
    try:
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(tmp_dir, 'audio.%(ext)s'),
            'quiet': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
            }],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
            file_path = os.path.join(tmp_dir, 'audio.mp3')

        async with Shazam() as shazam:
            out = await shazam.recognize(file_path)

        if not out.get("track"):
            await msg.edit_text("😔 Не удалось распознать песню по этой ссылке.", reply_markup=back_kb())
            return

        track = Serialize.full_track(out).track
        text = f"🎵 <b>{track.title}</b>\n👤 {track.subtitle}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("▶️ YouTube", url=track.youtube_link, style="danger")], [btn("◀️ Назад в меню", callback_data="main_menu", style="danger")]]) if track.youtube_link else back_kb()
        await msg.edit_text(text, reply_markup=kb)
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}", reply_markup=back_kb())
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

@router.message(UserStates.waiting_music, F.voice | F.audio | F.video)
async def handle_music_media(message: Message, state: FSMContext):
    msg = await message.answer("🎧 Слушаю...")
    tmp_dir = tempfile.mkdtemp()
    try:
        if message.voice:
            file_id = message.voice.file_id
            ext = ".ogg"
        elif message.audio:
            file_id = message.audio.file_id
            ext = ".mp3"
        else:
            file_id = message.video.file_id
            ext = ".mp4"

        file = await bot.get_file(file_id)
        input_path = os.path.join(tmp_dir, f"input{ext}")
        await bot.download_file(file.file_path, input_path)

        if message.video:
            audio_path = os.path.join(tmp_dir, "audio.mp3")
            os.system(f"ffmpeg -i {input_path} -q:a 0 -map a {audio_path} -y -loglevel quiet")
            process_path = audio_path
        else:
            process_path = input_path

        async with Shazam() as shazam:
            out = await shazam.recognize(process_path)

        if not out.get("track"):
            await msg.edit_text("😔 Не удалось распознать песню.", reply_markup=back_kb())
            return

        track = Serialize.full_track(out).track
        text = f"🎵 <b>{track.title}</b>\n👤 {track.subtitle}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("▶️ YouTube", url=track.youtube_link, style="danger")], [btn("◀️ Назад в меню", callback_data="main_menu", style="danger")]]) if track.youtube_link else back_kb()
        await msg.edit_text(text, reply_markup=kb)

    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}", reply_markup=back_kb())
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

# ---------- АДМИН-ПАНЕЛЬ ----------
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
    try:
        await message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=kb)
    except:
        await message.answer("🛠 <b>Админ-панель</b>", reply_markup=kb)

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
    await call.message.edit_text("Введи <b>название кнопки</b> (например, \"📢 Подписаться\"):", reply_markup=admin_cancel_kb())
    await state.set_state(AdminStates.sub_button_name)
    await call.answer()

@router.message(AdminStates.sub_button_name)
async def sub_button_name(message: Message, state: FSMContext):
    await state.update_data(sub_button_name=message.text)
    await message.answer("Теперь скинь <b>ссылку на канал</b> (например, @my_channel или https://t.me/my_channel):", reply_markup=admin_cancel_kb())
    await state.set_state(AdminStates.sub_channel_link)

@router.message(AdminStates.sub_channel_link)
async def sub_channel_link(message: Message, state: FSMContext):
    link = message.text.strip()
    channel_id = "@" + link.split("t.me/")[-1].split("/")[0] if "t.me/" in link else link
    await state.update_data(sub_channel_id=channel_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("⏭ Пропустить", callback_data="sub_skip_photo", style="success")],
        [btn("❌ Отмена", callback_data="admin_back", style="danger")]
    ])
    await message.answer("Теперь можешь отправить <b>фото</b> для красоты, или нажми \"Пропустить\".", reply_markup=kb)
    await state.set_state(AdminStates.sub_photo)

@router.message(AdminStates.sub_photo, F.photo)
async def sub_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    await set_setting("subscription", {
        "button_text": data["sub_button_name"],
        "channel_id": data["sub_channel_id"],
        "photo_id": message.photo[-1].file_id
    })
    await message.answer("✅ Подписка успешно настроена!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("◀️ В админку", callback_data="admin_back", style="danger")]]))
    await state.set_state(AdminStates.main_menu)

@router.callback_query(F.data == "sub_skip_photo")
async def sub_skip_photo(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await set_setting("subscription", {
        "button_text": data["sub_button_name"],
        "channel_id": data["sub_channel_id"],
        "photo_id": None
    })
    await call.message.edit_text("✅ Подписка настроена (без фото).", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("◀️ В админку", callback_data="admin_back", style="danger")]]))
    await state.set_state(AdminStates.main_menu)
    await call.answer()

@router.callback_query(F.data == "sub_delete")
async def sub_delete(call: CallbackQuery, state: FSMContext):
    await set_setting("subscription", {})
    await call.answer("Удалено!", show_alert=True)
    await admin_sub_menu(call, state)

# ---------- РАССЫЛКА ----------
@router.callback_query(F.data == "admin_broadcast_menu")
async def broadcast_menu(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("✅ С кнопкой", callback_data="broadcast_with_btn", style="success")],
        [btn("❌ Без кнопки", callback_data="broadcast_without_btn", style="danger")],
        [btn("◀️ Назад", callback_data="admin_back", style="danger")],
    ])
    await call.message.edit_text("📢 <b>Рассылка</b>\n\nВыбери тип:", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "broadcast_with_btn")
async def broadcast_with_btn(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("Отправь <b>текст</b> для рассылки (с кнопкой):", reply_markup=admin_cancel_kb())
    await state.set_state(AdminStates.broadcast_text)
    await call.answer()

@router.message(AdminStates.broadcast_text)
async def broadcast_text_with_btn(message: Message, state: FSMContext):
    await state.update_data(broadcast_text=message.text)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("⏭ Пропустить фото", callback_data="broadcast_skip_photo_btn", style="success")],
        [btn("❌ Отмена", callback_data="admin_back", style="danger")]
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

@router.callback_query(F.data == "broadcast_without_btn")
async def broadcast_without_btn(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("Отправь <b>что угодно</b> для рассылки:", reply_markup=admin_cancel_kb())
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

async def show_broadcast_preview(message: Message, state: FSMContext):
    data = await state.get_data()
    text = data.get("broadcast_text", "")
    photo_id = data.get("broadcast_photo")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("🚀 Отправить", callback_data="broadcast_confirm", style="success")],
        [btn("◀️ Назад", callback_data="admin_back", style="danger")],
    ])
    await message.answer("👀 <b>Предпросмотр:</b>")
    if photo_id: await message.answer_photo(photo_id, caption=text)
    else: await message.answer(text)
    await message.answer("Подтверди отправку:", reply_markup=kb)
    await state.set_state(AdminStates.broadcast_preview)

@router.callback_query(F.data == "broadcast_confirm")
async def broadcast_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_ids = await get_all_users()
    if not user_ids:
        await call.message.edit_text("❌ Нет пользователей.")
        await state.clear()
        return

    await call.message.edit_text(f"📤 Рассылка на {len(user_ids)} чел...")
    
    reply_markup = InlineKeyboardMarkup(inline_keyboard=[[btn("🔗 Перейти", url="https://t.me/durov", style="primary")]]) if "broadcast_text" in data else None

    success, fail = 0, 0
    for uid in user_ids:
        try:
            if "copy_message_id" in data:
                await bot.copy_message(chat_id=uid, from_chat_id=data["copy_from_chat"], message_id=data["copy_message_id"])
            else:
                if data.get("broadcast_photo"):
                    await bot.send_photo(uid, data["broadcast_photo"], caption=data["broadcast_text"], reply_markup=reply_markup)
                else:
                    await bot.send_message(uid, data["broadcast_text"], reply_markup=reply_markup)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1

    await call.message.edit_text(f"✅ Рассылка завершена.\nУспешно: {success}\nОшибок: {fail}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("◀️ В админку", callback_data="admin_back", style="danger")]]))
    await state.clear()

# ---------- HEALTH CHECK ----------
async def health(request):
    return web.Response(text="OK", status=200)

async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    port = int(os.environ.get("PORT", 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"✅ Health-check сервер слушает порт {port}")

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
