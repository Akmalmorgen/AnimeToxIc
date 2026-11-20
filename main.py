import asyncio
import html
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import aiohttp
import asyncpg
import yt_dlp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand, BotCommandScopeChat, CallbackQuery, FSInputFile,
    InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
    Message, ReplyKeyboardMarkup,
)
from shazamio import Shazam

# ---------- КОНФИГ ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TRACEMOE_TOKEN = os.getenv("TRACEMOE_TOKEN", "").strip()
SAUCENAO_KEY = os.getenv("SAUCENAO_KEY", "").strip()

SPAM_DELAY = 3
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024
TG_UPLOAD_LIMIT = 49 * 1024 * 1024
SC_CANDIDATES = 5
FFMPEG_OK = shutil.which("ffmpeg") is not None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")
esc = html.escape

if not BOT_TOKEN or not DATABASE_URL:
    print("❌ Заполни переменные окружения BOT_TOKEN и DATABASE_URL (Render → Environment).")
    sys.exit(1)
if not ADMIN_ID:
    print("⚠️ ADMIN_ID не задан — админка будет недоступна.")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
nav = Router()
admin = Router()
user = Router()
admin.message.filter(F.from_user.id == ADMIN_ID)
admin.callback_query.filter(F.from_user.id == ADMIN_ID)
dp.include_router(nav)
dp.include_router(admin)
dp.include_router(user)

db_pool: asyncpg.Pool | None = None
BOT_USERNAME = "bot"

# ---------- БРЕНД ----------
FANCY_BRAND = "𝓐𝓷𝓲𝓶𝓮𝓫𝓸𝓽𝟏𝓴"  # красивая подпись вместо чужих имён

# ---------- БАЗА ДАННЫХ ----------
async def _conn_init(conn: asyncpg.Connection):
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

async def create_pool() -> asyncpg.Pool:
    parts = urlsplit(DATABASE_URL)
    qs = parse_qs(parts.query)
    sslmode = qs.pop("sslmode", [None])[0]
    qs.pop("channel_binding", None)
    clean = urlunsplit(parts._replace(query=urlencode(qs, doseq=True)))
    ssl = "require" if sslmode in ("require", "verify-ca", "verify-full", "prefer") else None
    return await asyncpg.create_pool(clean, ssl=ssl, min_size=1, max_size=5, init=_conn_init)

async def init_db():
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT, first_name TEXT,
                joined_at TIMESTAMPTZ DEFAULT NOW(),
                last_seen TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value JSONB);
            CREATE TABLE IF NOT EXISTS track_cache (
                cache_key TEXT PRIMARY KEY,
                file_id TEXT NOT NULL, title TEXT, performer TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS anime_cache (
                file_unique_id TEXT PRIMARY KEY,
                payload JSONB, created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
    log.info("База данных готова")

async def get_setting(key: str):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key=$1", key)
        return row["value"] if row else None

async def set_setting(key: str, value: dict):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO settings (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = $2
        """, key, value)

async def add_user(uid: int, username: str | None, first_name: str | None):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username, first_name) VALUES ($1,$2,$3)
            ON CONFLICT (user_id) DO UPDATE
            SET username=EXCLUDED.username, first_name=EXCLUDED.first_name, last_seen=NOW()
        """, uid, username, first_name)

async def remove_user(uid: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE user_id=$1", uid)

async def get_all_users() -> list[int]:
    async with db_pool.acquire() as conn:
        return [r["user_id"] for r in await conn.fetch("SELECT user_id FROM users")]

async def get_cached_track(key: str):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM track_cache WHERE cache_key=$1", key)

async def cache_track(key: str, file_id: str, title: str, performer: str):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO track_cache (cache_key, file_id, title, performer) VALUES ($1,$2,$3,$4)
            ON CONFLICT (cache_key) DO UPDATE SET file_id=$2, title=$3, performer=$4
        """, key, file_id, title, performer)

async def get_cached_anime(fid: str):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT payload FROM anime_cache WHERE file_unique_id=$1", fid)
        return row["payload"] if row else None

async def cache_anime(fid: str, payload: dict):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO anime_cache (file_unique_id, payload) VALUES ($1,$2)
            ON CONFLICT (file_unique_id) DO UPDATE SET payload=$2
        """, fid, payload)

# ---------- ХЕЛПЕРЫ ----------
def btn(text, callback_data=None, url=None, style=None):
    return InlineKeyboardButton(text=text, callback_data=callback_data, url=url, style=style)

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (s or "").lower())).strip()

def fmt_dur(sec) -> str:
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"

def main_menu_reply(uid: int) -> ReplyKeyboardMarkup:
    kb = [[KeyboardButton(text="🖼 Поиск аниме"), KeyboardButton(text="🎵 Поиск музыки")]]
    if uid == ADMIN_ID:
        kb.append([KeyboardButton(text="🛠 Админка")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[btn("◀️ Назад в меню", callback_data="main_menu", style="danger")]])

def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Отмена", callback_data="main_menu", style="danger")]])

def admin_cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Отмена", callback_data="admin_back", style="danger")]])

async def safe_edit(msg: Message, text: str, **kw):
    try:
        await msg.edit_text(text, **kw)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 0.5)
        try: await msg.edit_text(text, **kw)
        except Exception: pass
    except Exception:
        pass

async def safe_delete(msg: Message):
    try: await msg.delete()
    except Exception: pass

async def run_ffmpeg(*args):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("ffmpeg: превышено время обработки")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg: {err.decode(errors='ignore')[-200:]}")

def tg_file_size(message: Message) -> int:
    if message.photo: return message.photo[-1].file_size or 0
    for obj in (message.video, message.video_note, message.voice, message.audio, message.document):
        if obj: return obj.file_size or 0
    return 0

NO_FFMPEG_TEXT = ("⚠️ Обработка видео на сервере пока недоступна.\n"
                  "Пришли голосовое, аудиофайл или название текстом.")

# ---------- АНТИСПАМ ----------
_spam: dict[int, float] = {}

def check_spam(uid: int) -> bool:
    now = time.time()
    if len(_spam) > 5000:
        for k in [k for k, v in _spam.items() if now - v > 60]:
            _spam.pop(k, None)
    if now - _spam.get(uid, 0) < SPAM_DELAY:
        return True
    _spam[uid] = now
    return False

# ---------- MIDDLEWARE ----------
_last_admin_notify = 0.0

class GateMiddleware:
    async def __call__(self, handler, event, data):
        global _last_admin_notify
        u = getattr(event, "from_user", None)
        if not u:
            return await handler(event, data)
        try:
            await add_user(u.id, u.username, u.first_name)
        except Exception as e:
            log.warning("add_user: %s", e)

        is_admin = u.id == ADMIN_ID
        is_start = isinstance(event, Message) and event.text and event.text.startswith("/start")
        is_check = isinstance(event, CallbackQuery) and event.data == "check_sub"
        if is_admin or is_start or is_check:
            return await handler(event, data)

        if check_spam(u.id):
            if isinstance(event, Message):
                await event.answer(f"⏳ Не так быстро, подожди {SPAM_DELAY} сек.")
            else:
                await event.answer("⏳ Слишком быстро!", show_alert=True)
            return

        sub = await get_setting("subscription")
        if not sub or not sub.get("channel_id"):
            return await handler(event, data)

        ok, error = False, False
        try:
            m = await bot.get_chat_member(sub["channel_id"], u.id)
            ok = m.status in ("creator", "administrator", "member") or (
                m.status == "restricted" and getattr(m, "is_member", False))
        except Exception as e:
            log.warning("sub check: %s", e)
            error = True
            if time.time() - _last_admin_notify > 1800 and ADMIN_ID:
                _last_admin_notify = time.time()
                try:
                    await bot.send_message(ADMIN_ID, f"⚠️ Не могу проверить подписку на {sub['channel_id']}.\n"
                                                     f"Сделай бота админом канала.\nОшибка: {esc(str(e))[:200]}")
                except Exception: pass

        if ok:
            return await handler(event, data)

        if error:
            text = "⚠️ Не могу проверить подписку. Админ уведомлён, попробуй позже."
            kb = InlineKeyboardMarkup(inline_keyboard=[[btn("🔄 Повторить", callback_data="check_sub", style="primary")]])
        else:
            text = "🔒 <b>Доступ закрыт!</b>\n\nПодпишись на канал, чтобы пользоваться ботом."
            rows = []
            if sub.get("button_url"):
                rows.append([btn(sub.get("button_text", "📢 Подписаться"), url=sub["button_url"], style="primary")])
            rows.append([btn("✅ Я подписался", callback_data="check_sub", style="success")])
            kb = InlineKeyboardMarkup(inline_keyboard=rows)

        if isinstance(event, CallbackQuery):
            await event.answer()
            target = event.message
        else:
            target = event
        if target is None:
            return
        try:
            if sub.get("photo_id") and not error:
                await target.answer_photo(sub["photo_id"], caption=text, reply_markup=kb)
            else:
                await target.answer(text, reply_markup=kb)
        except Exception:
            pass

dp.message.outer_middleware(GateMiddleware())
dp.callback_query.outer_middleware(GateMiddleware())

# ---------- СОСТОЯНИЯ ----------
class UserStates(StatesGroup):
    waiting_anime = State()
    waiting_music = State()

class AdminStates(StatesGroup):
    main = State()
    sub_name = State()
    sub_channel = State()
    sub_btn_url = State()
    sub_photo = State()
    bc_text = State()
    bc_photo = State()
    bc_btn_text = State()
    bc_btn_url = State()
    bc_preview = State()
    bc_copy = State()

# ---------- НАВИГАЦИЯ ----------
@nav.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        f"✨ Привет, <b>{esc(message.from_user.first_name or 'друг')}</b>!\n\n"
        "🖼 <b>Поиск аниме</b> — скинь скриншот или видео\n"
        "🎵 <b>Поиск музыки</b> — название, голосовое, аудио, видео или ссылка на клип.\n"
        "Распознаю песню и даю скачать полный трек прямо в чат.\n\n"
        "👇 Выбирай кнопку внизу:",
        reply_markup=main_menu_reply(message.from_user.id))

@nav.message(F.text == "🖼 Поиск аниме")
async def anime_menu(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(UserStates.waiting_anime)
    await message.answer(
        "🖼 <b>Поиск аниме по кадру</b>\n\n"
        "Отправь <b>скриншот</b>, <b>видео</b> (до 20 МБ) или ссылку на картинку.\n"
        "Если это фанарт — попробую найти источник арта.",
        reply_markup=back_kb())

@nav.message(F.text == "🎵 Поиск музыки")
async def music_menu(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(UserStates.waiting_music)
    await message.answer(
        "🎵 <b>Поиск музыки</b>\n\n"
        "📝 Название песни текстом\n"
        "🎤 Голосовое / аудио / видео\n"
        "🔗 Ссылка на клип (TikTok, Shorts, Instagram…)\n\n"
        "Распознаю песню и отправляю <b>полный трек файлом</b>.",
        reply_markup=cancel_kb())

@nav.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_delete(call.message)
    await call.message.answer("🏠 <b>Главное меню</b>\n\nВыбери действие:",
                              reply_markup=main_menu_reply(call.from_user.id))
    await call.answer()

@nav.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery, state: FSMContext):
    sub = await get_setting("subscription")
    if not sub or not sub.get("channel_id"):
        ok = True
    else:
        ok = False
        try:
            m = await bot.get_chat_member(sub["channel_id"], call.from_user.id)
            ok = m.status in ("creator", "administrator", "member") or (
                m.status == "restricted" and getattr(m, "is_member", False))
        except Exception:
            ok = False
    if ok:
        await call.answer("✅ Доступ открыт!")
        await safe_delete(call.message)
        await call.message.answer("🏠 <b>Главное меню</b>", reply_markup=main_menu_reply(call.from_user.id))
    else:
        await call.answer("❌ Подписка не найдена. Подпишись и нажми ещё раз.", show_alert=True)

# ---------- МУЗЫКА ----------
def _sc_search(query: str, title_hint: str | None = None) -> list[dict]:
    queries = [query]
    if title_hint:
        th = title_hint.strip()
        if th and norm(th) != norm(query):
            queries.append(th)
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "skip_download": True, "extract_flat": True, "socket_timeout": 30}
    seen, entries = set(), []
    for q in queries:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"scsearch{SC_CANDIDATES}:{q}", download=False)
        except Exception as e:
            log.warning("search: %s", e)
            continue
        for e in (info or {}).get("entries") or []:
            u = e.get("webpage_url") if e else None
            if u and u not in seen:
                seen.add(u)
                entries.append(e)
    want = norm(title_hint or query)
    want_words = [w for w in want.split() if len(w) > 2]
    qn = norm(query)
    out = []
    for e in entries:
        dur = e.get("duration") or 0
        if dur and dur < 35:
            continue
        et = norm(e.get("title") or "")
        eu = norm(e.get("uploader") or "")
        if title_hint:
            tsim = SequenceMatcher(None, want, et).ratio()
            overlap = (sum(1 for w in want_words if w in et) / len(want_words)) if want_words else 0
            match = max(tsim, overlap)
        else:
            match = SequenceMatcher(None, qn, f"{eu} {et}").ratio()
            if want_words and all(w in f"{eu} {et}" for w in want_words):
                match += 0.25
        out.append({"url": e["webpage_url"], "title": e.get("title") or "Трек",
                    "uploader": e.get("uploader") or "Исполнитель",
                    "duration": dur, "match": match})
    out.sort(key=lambda x: x["match"], reverse=True)
    return out

def _sc_download(url: str, tmp: str) -> tuple[Path, dict]:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 30,
            "retries": 2, "max_filesize": TG_UPLOAD_LIMIT,
            "format": "bestaudio[ext=mp3]/bestaudio/best",
            "outtmpl": os.path.join(tmp, "track.%(ext)s")}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = sorted(Path(tmp).glob("track.*"))
    if not files:
        raise RuntimeError("файл не скачался")
    return files[0], info or {}

def _clip_download(url: str, tmp: str) -> Path:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 30,
            "retries": 2, "max_filesize": 30 * 1024 * 1024,
            "format": "bestaudio/best", "outtmpl": os.path.join(tmp, "clip.%(ext)s")}
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    files = sorted(Path(tmp).glob("clip.*"))
    if not files:
        raise RuntimeError("не удалось скачать клип")
    return files[0]

async def shazam_recognize(path: Path) -> tuple[str, str] | None:
    try:
        async with Shazam() as sh:
            out = await sh.recognize(str(path))
    except Exception as e:
        log.warning("recognize: %s", e)
        return None
    track = getattr(out, "track", None)
    if track is None and isinstance(out, dict):
        track = out.get("track")
    if not track:
        return None
    get = (lambda k: track.get(k)) if isinstance(track, dict) else (lambda k: getattr(track, k, None))
    title, subtitle = get("title"), get("subtitle")
    if not title:
        return None
    return (subtitle or "").strip(), title.strip()

async def deliver_track(message: Message, state: FSMContext, cand: dict,
                        key: str, label: str | None, progress: Message) -> bool:
    tmp = tempfile.mkdtemp()
    try:
        await safe_edit(progress, "⬇️ <b>Скачиваю трек...</b>")
        path, info = await asyncio.to_thread(_sc_download, cand["url"], tmp)
        if path.stat().st_size > TG_UPLOAD_LIMIT:
            return False
        if path.suffix.lower() not in (".mp3", ".m4a"):
            if not FFMPEG_OK:
                return False
            mp3 = path.with_suffix(".mp3")
            await run_ffmpeg("-i", str(path), "-vn", "-c:a", "libmp3lame", "-b:a", "192k", str(mp3))
            path = mp3
        title = info.get("title") or cand["title"]
        duration = int(info.get("duration") or cand["duration"] or 0)
        caption = (f"🎵 <b>{esc(title)}</b>\n👤 {FANCY_BRAND}\n⏱ {fmt_dur(duration)}\n\n"
                   f"📥 @{BOT_USERNAME}")
        if label:
            caption = f"🎧 <b>Распознано:</b> {esc(label)}\n\n" + caption
        rows = []
        data = await state.get_data()
        if len(data.get("sc_candidates") or []) > 1:
            rows.append([btn("🔁 Другая версия", callback_data="sc_alt", style="primary")])
        rows.append([btn("◀️ В меню", callback_data="main_menu", style="danger")])
        sent = await message.answer_audio(
            audio=FSInputFile(str(path)), title=title[:64], performer=FANCY_BRAND,
            duration=duration, caption=caption,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        if sent.audio:
            await cache_track(key, sent.audio.file_id, title, FANCY_BRAND)
        await safe_delete(progress)
        return True
    except Exception as e:
        log.warning("deliver_track: %s", e)
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

async def process_music(message: Message, state: FSMContext, query: str,
                        label: str | None = None, progress: Message | None = None):
    progress = progress or await message.answer("🔎 <b>Ищу трек...</b>")
    key = norm(query)
    cached = await get_cached_track(key)
    if cached:
        caption = (f"🎵 <b>{esc(cached['title'] or 'Трек')}</b>\n👤 {FANCY_BRAND}\n\n"
                   f"📥 @{BOT_USERNAME}")
        if label:
            caption = f"🎧 <b>Распознано:</b> {esc(label)}\n\n" + caption
        await message.answer_audio(audio=cached["file_id"], title=(cached["title"] or "")[:64],
                                   performer=FANCY_BRAND, caption=caption,
                                   reply_markup=back_kb())
        await safe_delete(progress)
        return
    await safe_edit(progress, "🔎 <b>Ищу трек...</b>")
    title_hint = label.split(" – ", 1)[1].strip() if label and " – " in label else None
    try:
        candidates = await asyncio.to_thread(_sc_search, query, title_hint)
    except Exception as e:
        log.warning("search: %s", e)
        candidates = []
    if title_hint:
        candidates = [c for c in candidates if c.get("match", 0) >= 0.5]
    if not candidates:
        text = (f"🎧 <b>Распознано:</b> {esc(label)}\n\n" if label else "")
        text += "😔 Не нашёл именно этот трек. Попробуй ввести название вручную."
        await safe_edit(progress, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [btn("✏️ Ввести название", callback_data="sc_manual", style="primary")],
            [btn("◀️ В меню", callback_data="main_menu", style="danger")]]))
        return
    await state.update_data(sc_candidates=candidates, sc_index=0, sc_key=key, sc_label=label)
    for i, cand in enumerate(candidates[:3]):
        await state.update_data(sc_index=i)
        if await deliver_track(message, state, cand, key, label, progress):
            return
    await safe_edit(progress, "😔 Не удалось скачать ни одну версию. Попробуй другое название.",
                    reply_markup=back_kb())

@user.callback_query(F.data == "sc_alt")
async def cb_alt_version(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cands = data.get("sc_candidates") or []
    idx = int(data.get("sc_index", 0)) + 1
    if not cands or idx >= len(cands):
        return await call.answer("Больше версий нет 🤷", show_alert=True)
    await call.answer("Ищу другую версию...")
    await state.update_data(sc_index=idx)
    progress = await call.message.answer("⬇️ <b>Скачиваю другую версию...</b>")
    ok = await deliver_track(call.message, state, cands[idx], data.get("sc_key", ""),
                             data.get("sc_label"), progress)
    if not ok:
        await safe_edit(progress, "😔 Эта версия не скачалась. Попробуй ещё раз или введи название.",
                        reply_markup=back_kb())

@user.callback_query(F.data == "sc_manual")
async def cb_manual(call: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_music)
    await call.answer()
    await call.message.answer("✏️ Напиши название песни (можно «артист – название»):",
                              reply_markup=cancel_kb())

@user.message(UserStates.waiting_music, F.text)
async def music_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text.startswith(("http://", "https://")):
        if not FFMPEG_OK:
            return await message.answer(NO_FFMPEG_TEXT, reply_markup=back_kb())
        progress = await message.answer("🔗 <b>Скачиваю клип и слушаю...</b>")
        tmp = tempfile.mkdtemp()
        try:
            clip = await asyncio.to_thread(_clip_download, text, tmp)
            audio = Path(tmp) / "audio.mp3"
            await run_ffmpeg("-i", str(clip), "-vn", "-ac", "1", "-ar", "16000",
                             "-c:a", "libmp3lame", "-b:a", "128k", str(audio))
            await safe_edit(progress, "🎧 <b>Распознаю...</b>")
            res = await shazam_recognize(audio)
            if not res:
                return await safe_edit(progress, "😔 Не удалось распознать песню из клипа.",
                                       reply_markup=back_kb())
            artist, title = res
            label = f"{artist} – {title}" if artist else title
            await process_music(message, state, f"{artist} {title}".strip(), label, progress)
        except Exception as e:
            log.warning("clip: %s", e)
            await safe_edit(progress, "❌ Не удалось скачать клип по ссылке. "
                                      "Пришли видео файлом (до 20 МБ) или название текстом.",
                            reply_markup=back_kb())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return
    await process_music(message, state, text)

@user.message(UserStates.waiting_music, F.voice | F.audio | F.video | F.video_note | F.document)
async def music_media(message: Message, state: FSMContext):
    if tg_file_size(message) > TG_DOWNLOAD_LIMIT:
        return await message.answer("⚠️ Файл больше 20 МБ — Telegram не даёт ботам такие скачивать. "
                                    "Пришли файл поменьше или ссылку на клип.", reply_markup=back_kb())
    doc_mime = (message.document.mime_type or "") if message.document else ""
    if message.document and not doc_mime.startswith(("audio/", "video/")):
        return await message.answer("Пришли аудио, видео или голосовое 🙂", reply_markup=cancel_kb())
    is_video_input = bool(message.video or message.video_note or doc_mime.startswith("video/"))
    if is_video_input and not FFMPEG_OK:
        return await message.answer(NO_FFMPEG_TEXT, reply_markup=back_kb())
    progress = await message.answer("🎧 <b>Слушаю...</b>")
    tmp = tempfile.mkdtemp()
    try:
        if message.voice: fid, ext = message.voice.file_id, ".ogg"
        elif message.audio: fid, ext = message.audio.file_id, ".mp3"
        elif message.video: fid, ext = message.video.file_id, ".mp4"
        elif message.video_note: fid, ext = message.video_note.file_id, ".mp4"
        else: fid, ext = message.document.file_id, ".bin"
        file = await bot.get_file(fid)
        src = Path(tmp) / f"input{ext}"
        await bot.download_file(file.file_path, destination=str(src))
        need_convert = bool(is_video_input or ext not in (".ogg", ".mp3", ".m4a", ".wav", ".flac"))
        process_path = src
        if need_convert:
            await safe_edit(progress, "🎬 <b>Извлекаю звук...</b>")
            process_path = Path(tmp) / "audio.mp3"
            await run_ffmpeg("-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                             "-c:a", "libmp3lame", "-b:a", "128k", str(process_path))
        await safe_edit(progress, "🎧 <b>Распознаю...</b>")
        res = await shazam_recognize(process_path)
        if not res:
            return await safe_edit(progress, "😔 Не удалось распознать песню. "
                                             "Попробуй запись подлиннее или напиши название.",
                                   reply_markup=back_kb())
        artist, title = res
        label = f"{artist} – {title}" if artist else title
        await process_music(message, state, f"{artist} {title}".strip(), label, progress)
    except Exception as e:
        log.warning("music_media: %s", e)
        await safe_edit(progress, f"❌ Ошибка: {esc(str(e))[:200]}", reply_markup=back_kb())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# ---------- АНИМЕ ----------
async def trace_search(session: aiohttp.ClientSession, data: bytes | None = None, url: str | None = None):
    params = {"anilistInfo": "1", "cutBorders": "1"}
    headers = {"x-trace-key": TRACEMOE_TOKEN} if TRACEMOE_TOKEN else {}
    kwargs = {"params": params, "headers": headers, "timeout": aiohttp.ClientTimeout(total=25)}
    if url:
        params["url"] = url
        resp_cm = session.post("https://api.trace.moe/search", **kwargs)
    else:
        form = aiohttp.FormData()
        form.add_field("file", data, filename="image.jpg", content_type="image/jpeg")
        resp_cm = session.post("https://api.trace.moe/search", data=form, **kwargs)
    async with resp_cm as resp:
        if resp.status in (402, 429):
            return {"error": "quota"}
        if resp.status != 200:
            return {"error": f"HTTP {resp.status}"}
        return await resp.json()

async def anilist_cover(session, anilist_id: int) -> str | None:
    query = "query($id:Int){Media(id:$id,type:ANIME){coverImage{large}}}"
    try:
        async with session.post("https://graphql.anilist.co",
                                json={"query": query, "variables": {"id": anilist_id}},
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
            j = await r.json()
            return j["data"]["Media"]["coverImage"]["large"]
    except Exception:
        return None

async def ru_title(session, title: str, year: int | None) -> str:
    try:
        async with session.get("https://shikimori.one/api/animes",
                               params={"search": title, "limit": 5},
                               headers={"User-Agent": "AnimeSearchBot"},
                               timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return title
            items = await r.json()
        if not items:
            return title
        if year:
            for it in items:
                if (it.get("aired_on") or "").startswith(str(year)):
                    return it.get("russian") or title
        return items[0].get("russian") or title
    except Exception:
        return title

async def saucenao_search(session, data: bytes):
    if not SAUCENAO_KEY:
        return None
    try:
        form = aiohttp.FormData()
        form.add_field("file", data, filename="image.jpg", content_type="image/jpeg")
        async with session.post("https://saucenao.com/search.php",
                                params={"db": "999", "output_type": "2", "numres": "3", "api_key": SAUCENAO_KEY},
                                data=form, timeout=aiohttp.ClientTimeout(total=20)) as r:
            j = await r.json()
        for res in j.get("results") or []:
            sim = float(res.get("header", {}).get("similarity", 0))
            if sim >= 60:
                d = res.get("data", {})
                return {"sim": sim, "title": d.get("title") or d.get("member_name") or "Арт",
                        "urls": (d.get("ext_urls") or [])[:2], "index": res.get("header", {}).get("index_name", "")}
    except Exception as e:
        log.warning("art search: %s", e)
    return None

async def render_anime_payload(message: Message, payload: dict):
    if payload["kind"] == "anime":
        if payload.get("photo"):
            try:
                await message.answer_photo(payload["photo"], caption=payload["text"])
            except Exception:
                await message.answer(payload["text"])
        else:
            await message.answer(payload["text"])
        if payload.get("video"):
            try: await message.answer_video(payload["video"], caption="🎬 Отрывок из аниме")
            except Exception: pass
    elif payload["kind"] == "art":
        rows = [[btn(f"🔗 Источник {i+1}", url=u, style="primary")] for i, u in enumerate(payload["urls"])]
        rows.append([btn("◀️ В меню", callback_data="main_menu", style="danger")])
        await message.answer(payload["text"], reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    else:
        await message.answer(payload["text"], reply_markup=back_kb())

async def do_anime_search(message: Message, frames: list[bytes], fid: str | None, progress: Message):
    async with aiohttp.ClientSession() as session:
        best, best_sim = None, 0.0
        for frame in frames:
            res = await trace_search(session, data=frame)
            if res.get("error") == "quota":
                return await safe_edit(progress, "⚠️ Дневной лимит поиска аниме исчерпан. Попробуй завтра.",
                                       reply_markup=back_kb())
            for r in res.get("result") or []:
                if r.get("similarity", 0) > best_sim:
                    best, best_sim = r, r["similarity"]
            if best_sim >= 0.9:
                break
        if best and best_sim >= 0.5:
            await safe_edit(progress, "🌐 <b>Перевожу название...</b>")
            ani = best.get("anilist") if isinstance(best.get("anilist"), dict) else {}
            titles = ani.get("title") or {}
            orig = titles.get("english") or titles.get("romaji") or best.get("filename", "Неизвестно")
            year = ani.get("seasonYear")
            ru = await ru_title(session, orig, year) if orig else "Неизвестно"
            cover = await anilist_cover(session, ani["id"]) if ani.get("id") else None
            sim = best_sim * 100
            warn = "\n\n⚠️ Схожесть низкая — бот может ошибаться." if sim < 87 else ""
            text = (f"🎬 <b>Аниме:</b> {esc(ru)}\n"
                    f"🇯🇵 <b>Оригинал:</b> {esc(orig)}\n"
                    f"📺 <b>Серия:</b> {esc(str(best.get('episode', '?')))}\n"
                    f"⏱ <b>Таймкод:</b> {int(best.get('from', 0))}–{int(best.get('to', 0))} сек.\n"
                    f"📊 <b>Схожесть:</b> {sim:.1f}%{warn}")
            payload = {"kind": "anime", "text": text,
                       "photo": cover or best.get("image"), "video": best.get("video")}
            if fid: await cache_anime(fid, payload)
            await render_anime_payload(message, payload)
            await safe_delete(progress)
            return
        await safe_edit(progress, "🎨 <b>Ищу источник арта...</b>")
        art = await saucenao_search(session, frames[0]) if frames else None
        if art:
            text = (f"🎨 <b>Похоже на арт/фанарт</b>\n\n"
                    f"🖼 {esc(art['title'])}\n📚 {esc(art['index'])}\n📊 Схожесть: {art['sim']:.0f}%")
            payload = {"kind": "art", "text": text, "urls": art["urls"]}
            if fid: await cache_anime(fid, payload)
            await render_anime_payload(message, payload)
            await safe_delete(progress)
        else:
            await safe_edit(progress, "😔 <b>Ничего не найдено.</b>\n\nВозможно, это фанарт, AI-арт, "
                                      "кадр сильно обрезан или серия ещё не в базе. Попробуй другой кадр.",
                            reply_markup=back_kb())

@user.message(UserStates.waiting_anime, F.photo | F.video | F.document)
async def anime_media(message: Message, state: FSMContext):
    if tg_file_size(message) > TG_DOWNLOAD_LIMIT:
        return await message.answer("⚠️ Файл больше 20 МБ — бот не может его скачать. "
                                    "Пришли скриншот или короткий клип.", reply_markup=back_kb())
    doc_mime = (message.document.mime_type or "") if message.document else ""
    if message.document and not doc_mime.startswith(("image/", "video/")):
        return await message.answer("Пришли фото или видео 🙂", reply_markup=cancel_kb())
    if message.photo:
        fid, unique, is_video = message.photo[-1].file_id, message.photo[-1].file_unique_id, False
    elif message.video:
        fid, unique, is_video = message.video.file_id, message.video.file_unique_id, True
    else:
        fid, unique = message.document.file_id, message.document.file_unique_id
        is_video = doc_mime.startswith("video/")
    if is_video and not FFMPEG_OK:
        return await message.answer("⚠️ Поиск по видео пока недоступен. Пришли скриншот.",
                                    reply_markup=back_kb())
    cached = await get_cached_anime(unique)
    if cached:
        return await render_anime_payload(message, cached)
    progress = await message.answer("🔍 <b>Ищу...</b>")
    tmp = tempfile.mkdtemp()
    try:
        file = await bot.get_file(fid)
        src = os.path.join(tmp, "input")
        await bot.download_file(file.file_path, destination=src)
        frames: list[bytes] = []
        if is_video:
            await safe_edit(progress, "🎬 <b>Извлекаю кадры...</b>")
            for ts in (1, 3, 5, 7, 9):
                out = os.path.join(tmp, f"f{ts}.jpg")
                try:
                    await run_ffmpeg("-ss", str(ts), "-i", src, "-frames:v", "1", "-q:v", "2", out)
                except Exception:
                    continue
                if os.path.exists(out) and os.path.getsize(out) > 0:
                    frames.append(Path(out).read_bytes())
            if not frames:
                out = os.path.join(tmp, "f0.jpg")
                try:
                    await run_ffmpeg("-i", src, "-frames:v", "1", "-q:v", "2", out)
                    if os.path.exists(out): frames.append(Path(out).read_bytes())
                except Exception: pass
        else:
            frames.append(Path(src).read_bytes())
        if not frames:
            return await safe_edit(progress, "❌ Не удалось извлечь кадры из файла.", reply_markup=back_kb())
        await do_anime_search(message, frames, unique, progress)
    except Exception as e:
        log.warning("anime_media: %s", e)
        await safe_edit(progress, f"❌ Ошибка: {esc(str(e))[:200]}", reply_markup=back_kb())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

@user.message(UserStates.waiting_anime, F.text)
async def anime_link(message: Message, state: FSMContext):
    url = (message.text or "").strip()
    if not url.startswith(("http://", "https://")):
        return await message.answer("Пришли скриншот, видео или ссылку на картинку 🙂", reply_markup=cancel_kb())
    if not re.search(r"\.(jpe?g|png|webp|gif)(\?|$)", url, re.I):
        return await message.answer("По ссылке должна быть картинка. Для клипов используй «🎵 Поиск музыки».",
                                    reply_markup=cancel_kb())
    progress = await message.answer("🔍 <b>Ищу по ссылке...</b>")
    async with aiohttp.ClientSession() as session:
        res = await trace_search(session, url=url)
        if res.get("error") == "quota":
            return await safe_edit(progress, "⚠️ Дневной лимит поиска исчерпан. Попробуй завтра.",
                                   reply_markup=back_kb())
        results = res.get("result") or []
        if not results:
            return await safe_edit(progress, "😔 Ничего не найдено по ссылке.", reply_markup=back_kb())
        best = results[0]
        ani = best.get("anilist") if isinstance(best.get("anilist"), dict) else {}
        titles = ani.get("title") or {}
        orig = titles.get("english") or titles.get("romaji") or best.get("filename", "?")
        ru = await ru_title(session, orig, ani.get("seasonYear"))
    sim = best.get("similarity", 0) * 100
    await safe_edit(progress, f"🎬 <b>Аниме:</b> {esc(ru)}\n🇯🇵 {esc(orig)}\n"
                              f"📺 Серия: {esc(str(best.get('episode', '?')))}\n"
                              f"⏱ Таймкод: {int(best.get('from', 0))} сек.\n📊 Схожесть: {sim:.1f}%",
                    reply_markup=back_kb())

# ---------- АДМИНКА ----------
async def render_admin_panel(msg: Message, edit: bool):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("📢 Рассылка", callback_data="bc_menu", style="primary")],
        [btn("🔒 Обязательная подписка", callback_data="sub_menu", style="success")],
        [btn("🗑 Очистить кэш", callback_data="clear_cache", style="danger")],
        [btn("◀️ В меню", callback_data="main_menu", style="danger")]])
    text = "🛠 <b>Админ-панель</b>\n\nВыбери раздел:"
    if edit:
        try:
            await msg.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await msg.answer(text, reply_markup=kb)

@admin.message(F.text == "🛠 Админка")
@admin.message(Command("admin"))
async def admin_entry(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AdminStates.main)
    await render_admin_panel(message, edit=False)

@admin.message(Command("clearcache"))
async def cmd_clearcache(message: Message, state: FSMContext):
    """Очистка кэша треков и аниме. Только для админа."""
    try:
        async with db_pool.acquire() as conn:
            tracks = await conn.fetchval("SELECT COUNT(*) FROM track_cache")
            animes = await conn.fetchval("SELECT COUNT(*) FROM anime_cache")
            await conn.execute("TRUNCATE TABLE track_cache, anime_cache;")
        await message.answer(
            f"✅ <b>Кэш успешно очищен!</b>\n\n"
            f"🗑 Удалено треков: <b>{tracks}</b>\n"
            f"🗑 Удалено аниме: <b>{animes}</b>\n\n"
            f"Теперь все новые запросы будут сохраняться с обновлённой подписью.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [btn("◀️ В админку", callback_data="admin_back", style="danger")]
            ])
        )
    except Exception as e:
        log.error("Ошибка очистки кэша: %s", e)
        await message.answer(
            f"❌ Ошибка при очистке кэша: {esc(str(e))[:200]}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [btn("◀️ В админку", callback_data="admin_back", style="danger")]
            ])
        )

@admin.callback_query(F.data == "clear_cache")
async def cb_clear_cache(call: CallbackQuery, state: FSMContext):
    """Очистка кэша через кнопку в админке."""
    await call.answer("Очищаю кэш...")
    try:
        async with db_pool.acquire() as conn:
            tracks = await conn.fetchval("SELECT COUNT(*) FROM track_cache")
            animes = await conn.fetchval("SELECT COUNT(*) FROM anime_cache")
            await conn.execute("TRUNCATE TABLE track_cache, anime_cache;")
        await call.message.edit_text(
            f"✅ <b>Кэш успешно очищен!</b>\n\n"
            f"🗑 Удалено треков: <b>{tracks}</b>\n"
            f"🗑 Удалено аниме: <b>{animes}</b>\n\n"
            f"Теперь все новые запросы будут сохраняться с обновлённой подписью.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [btn("◀️ В админку", callback_data="admin_back", style="danger")]
            ])
        )
    except Exception as e:
        log.error("Ошибка очистки кэша: %s", e)
        await call.message.edit_text(
            f"❌ Ошибка при очистке кэша: {esc(str(e))[:200]}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [btn("◀️ В админку", callback_data="admin_back", style="danger")]
            ])
        )

@admin.callback_query(F.data == "admin_back")
async def admin_back(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AdminStates.main)
    await render_admin_panel(call.message, edit=True)
    await call.answer()

@admin.callback_query(F.data == "sub_menu")
async def sub_menu(call: CallbackQuery, state: FSMContext):
    sub = await get_setting("subscription")
    status = "✅ настроена" if sub and sub.get("channel_id") else "❌ не настроена"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("✏️ Настроить", callback_data="sub_setup", style="primary")],
        [btn("🗑 Удалить", callback_data="sub_delete", style="danger")],
        [btn("◀️ Назад", callback_data="admin_back", style="danger")]])
    await call.message.edit_text(f"🔒 <b>Обязательная подписка</b>\n\nСтатус: {status}\n\n"
                                 "Бот должен быть <b>админом</b> канала.", reply_markup=kb)
    await call.answer()

@admin.callback_query(F.data == "sub_setup")
async def sub_setup(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AdminStates.sub_name)
    await call.message.edit_text("📝 <b>Шаг 1/4:</b> название кнопки (например «📢 Подписаться»):",
                                 reply_markup=admin_cancel_kb())
    await call.answer()

@admin.message(AdminStates.sub_name, F.text)
async def sub_name(message: Message, state: FSMContext):
    await state.update_data(sub_button_text=message.text.strip()[:64])
    await state.set_state(AdminStates.sub_channel)
    await message.answer("📝 <b>Шаг 2/4:</b> канал для проверки: @username, публичная ссылка или ID (-100…).\n"
                         "Инвайт-ссылку пришлёшь на следующем шаге.", reply_markup=admin_cancel_kb())

def parse_channel(text: str) -> str | None:
    t = text.strip()
    if re.fullmatch(r"-100\d+", t): return t
    if "t.me/" in t:
        tail = t.split("t.me/")[-1].split("/")[0].split("?")[0]
        if tail.startswith("+") or tail.startswith("joinchat"): return None
        return "@" + tail.lstrip("@")
    if t.startswith("@"): return t
    if t.isdigit(): return "-100" + t
    return None

@admin.message(AdminStates.sub_channel, F.text)
async def sub_channel(message: Message, state: FSMContext):
    ch = parse_channel(message.text)
    if not ch:
        return await message.answer("⚠️ По инвайт-ссылке проверить нельзя. Пришли @username или ID (-100…).",
                                    reply_markup=admin_cancel_kb())
    try:
        me = await bot.get_chat_member(ch, bot.id)
        if me.status not in ("administrator", "creator"):
            return await message.answer("⚠️ Бот не админ этого канала. Добавь админом и пришли канал ещё раз.",
                                        reply_markup=admin_cancel_kb())
    except Exception as e:
        return await message.answer(f"⚠️ Не вижу канал: {esc(str(e))[:150]}\nПроверь и пришли ещё раз.",
                                    reply_markup=admin_cancel_kb())
    await state.update_data(sub_channel_id=ch)
    await state.set_state(AdminStates.sub_btn_url)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("⏭ Авто из username", callback_data="sub_url_skip", style="success")],
        [btn("❌ Отмена", callback_data="admin_back", style="danger")]])
    await message.answer("📝 <b>Шаг 3/4:</b> ссылка для кнопки (инвайт или публичная), либо «Авто».",
                         reply_markup=kb)

@admin.callback_query(F.data == "sub_url_skip")
async def sub_url_skip(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    ch = data.get("sub_channel_id", "")
    url = f"https://t.me/{ch.lstrip('@')}" if ch.startswith("@") else None
    await state.update_data(sub_button_url=url)
    await state.set_state(AdminStates.sub_photo)
    await call.message.edit_text("📝 <b>Шаг 4/4:</b> фото для сообщения подписки или «Пропустить».",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                     [btn("⏭ Пропустить", callback_data="sub_photo_skip", style="success")],
                                     [btn("❌ Отмена", callback_data="admin_back", style="danger")]]))
    await call.answer()

@admin.message(AdminStates.sub_btn_url, F.text)
async def sub_btn_url(message: Message, state: FSMContext):
    url = message.text.strip()
    if not url.startswith(("http://", "https://")):
        return await message.answer("Пришли ссылку http(s)://…", reply_markup=admin_cancel_kb())
    await state.update_data(sub_button_url=url)
    await state.set_state(AdminStates.sub_photo)
    await message.answer("📝 <b>Шаг 4/4:</b> фото или «Пропустить».",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [btn("⏭ Пропустить", callback_data="sub_photo_skip", style="success")],
                             [btn("❌ Отмена", callback_data="admin_back", style="danger")]]))

async def finish_sub_setup(message: Message, state: FSMContext, photo_id: str | None, edit: bool):
    data = await state.get_data()
    await set_setting("subscription", {
        "button_text": data.get("sub_button_text", "📢 Подписаться"),
        "channel_id": data.get("sub_channel_id"),
        "button_url": data.get("sub_button_url"),
        "photo_id": photo_id})
    await state.clear()
    await state.set_state(AdminStates.main)
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("◀️ В админку", callback_data="admin_back", style="danger")]])
    text = "✅ <b>Подписка настроена!</b>"
    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)

@admin.message(AdminStates.sub_photo, F.photo)
async def sub_photo(message: Message, state: FSMContext):
    await finish_sub_setup(message, state, message.photo[-1].file_id, edit=False)

@admin.callback_query(F.data == "sub_photo_skip")
async def sub_photo_skip(call: CallbackQuery, state: FSMContext):
    await finish_sub_setup(call.message, state, None, edit=True)
    await call.answer()

@admin.callback_query(F.data == "sub_delete")
async def sub_delete(call: CallbackQuery, state: FSMContext):
    await set_setting("subscription", {})
    await call.answer("🗑 Удалено!", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("✏️ Настроить", callback_data="sub_setup", style="primary")],
        [btn("◀️ Назад", callback_data="admin_back", style="danger")]])
    await call.message.edit_text("🔒 <b>Подписка</b>\n\nСтатус: ❌ не настроена", reply_markup=kb)

# ---------- РАССЫЛКА ----------
@admin.callback_query(F.data == "bc_menu")
async def bc_menu(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("✅ С кнопкой", callback_data="bc_with", style="success")],
        [btn("❌ Без кнопки", callback_data="bc_without", style="danger")],
        [btn("◀️ Назад", callback_data="admin_back", style="danger")]])
    await call.message.edit_text("📢 <b>Рассылка</b>\n\nВыбери тип:", reply_markup=kb)
    await call.answer()

@admin.callback_query(F.data == "bc_with")
async def bc_with(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AdminStates.bc_text)
    await call.message.edit_text("✍️ Отправь <b>текст</b> рассылки:", reply_markup=admin_cancel_kb())
    await call.answer()

@admin.message(AdminStates.bc_text, F.text)
async def bc_text(message: Message, state: FSMContext):
    await state.update_data(bc_text=message.text)
    await state.set_state(AdminStates.bc_photo)
    await message.answer("🖼 Добавь <b>фото</b> или пропусти:",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [btn("⏭ Пропустить фото", callback_data="bc_photo_skip", style="success")],
                             [btn("❌ Отмена", callback_data="admin_back", style="danger")]]))

@admin.message(AdminStates.bc_photo, F.photo)
async def bc_photo(message: Message, state: FSMContext):
    await state.update_data(bc_photo=message.photo[-1].file_id)
    await state.set_state(AdminStates.bc_btn_text)
    await message.answer("🔘 Теперь <b>текст кнопки</b>:", reply_markup=admin_cancel_kb())

@admin.callback_query(F.data == "bc_photo_skip")
async def bc_photo_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(bc_photo=None)
    await state.set_state(AdminStates.bc_btn_text)
    await call.message.edit_text("🔘 Теперь <b>текст кнопки</b>:", reply_markup=admin_cancel_kb())
    await call.answer()

@admin.message(AdminStates.bc_btn_text, F.text)
async def bc_btn_text(message: Message, state: FSMContext):
    await state.update_data(bc_btn_text=message.text.strip()[:64])
    await state.set_state(AdminStates.bc_btn_url)
    await message.answer("🔗 Теперь <b>ссылка кнопки</b> (http(s)://…):", reply_markup=admin_cancel_kb())

@admin.message(AdminStates.bc_btn_url, F.text)
async def bc_btn_url(message: Message, state: FSMContext):
    url = message.text.strip()
    if not url.startswith(("http://", "https://")):
        return await message.answer("Нужна ссылка http(s)://…", reply_markup=admin_cancel_kb())
    await state.update_data(bc_btn_url=url)
    await show_bc_preview(message, state)

async def show_bc_preview(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.set_state(AdminStates.bc_preview)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("🚀 Отправить", callback_data="bc_confirm", style="success")],
        [btn("❌ Отмена", callback_data="admin_back", style="danger")]])
    markup = None
    if data.get("bc_btn_text") and data.get("bc_btn_url"):
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [btn(data["bc_btn_text"], url=data["bc_btn_url"], style="primary")]])
    await message.answer("👀 <b>Предпросмотр:</b>")
    if data.get("bc_photo"):
        await message.answer_photo(data["bc_photo"], caption=data.get("bc_text", ""), reply_markup=markup)
    else:
        await message.answer(data.get("bc_text", ""), reply_markup=markup)
    await message.answer("Подтверди отправку:", reply_markup=kb)

@admin.callback_query(F.data == "bc_without")
async def bc_without(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AdminStates.bc_copy)
    await call.message.edit_text("✍️ Отправь <b>что угодно</b> (текст, фото, видео, стикер) — скопирую всем:",
                                 reply_markup=admin_cancel_kb())
    await call.answer()

@admin.message(AdminStates.bc_copy)
async def bc_copy(message: Message, state: FSMContext):
    await state.update_data(copy_message_id=message.message_id, copy_from_chat=message.chat.id)
    await state.set_state(AdminStates.bc_preview)
    await message.answer("👀 <b>Предпросмотр:</b>")
    await message.copy_to(chat_id=message.chat.id)
    await message.answer("Подтверди отправку:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [btn("🚀 Отправить", callback_data="bc_confirm", style="success")],
        [btn("❌ Отмена", callback_data="admin_back", style="danger")]]))

@admin.callback_query(F.data == "bc_confirm")
async def bc_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("copy_message_id") and not data.get("bc_text"):
        return await call.answer("Нет данных для рассылки, начни заново.", show_alert=True)
    users = await get_all_users()
    if not users:
        return await call.answer("Нет пользователей", show_alert=True)
    await call.answer()
    await call.message.edit_text(f"📤 Рассылка на {len(users)} чел...")
    markup = None
    if data.get("bc_btn_text") and data.get("bc_btn_url"):
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [btn(data["bc_btn_text"], url=data["bc_btn_url"], style="primary")]])
    ok = fail = 0
    for i, uid in enumerate(users):
        for attempt in (1, 2):
            try:
                if data.get("copy_message_id"):
                    await bot.copy_message(uid, data["copy_from_chat"], data["copy_message_id"])
                elif data.get("bc_photo"):
                    await bot.send_photo(uid, data["bc_photo"], caption=data.get("bc_text", ""), reply_markup=markup)
                else:
                    await bot.send_message(uid, data.get("bc_text", ""), reply_markup=markup)
                ok += 1
                break
            except TelegramRetryAfter as e:
                if attempt == 1:
                    await asyncio.sleep(e.retry_after + 1)
                else:
                    fail += 1
            except TelegramForbiddenError:
                await remove_user(uid)
                fail += 1
                break
            except TelegramBadRequest as e:
                if "deactivated" in str(e).lower():
                    await remove_user(uid)
                fail += 1
                break
            except Exception:
                fail += 1
                break
        if i % 20 == 0:
            await safe_edit(call.message, f"📤 Рассылка... {i}/{len(users)}\n✅ {ok} · ❌ {fail}")
        await asyncio.sleep(0.05)
    await state.clear()
    await state.set_state(AdminStates.main)
    await safe_edit(call.message, f"✅ <b>Рассылка завершена.</b>\n\nУспешно: {ok}\nОшибок: {fail}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [btn("◀️ В админку", callback_data="admin_back", style="danger")]]))

# ---------- HEALTH-CHECK ----------
async def start_health_server():
    app = web.Application()
    async def ok(_): return web.Response(text="OK")
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Health-сервер на порту %s", port)

# ---------- ЗАПУСК ----------
async def main():
    global db_pool, BOT_USERNAME
    log.info("ffmpeg: %s", "найден" if FFMPEG_OK else "не найден (видео-функции отключены)")
    db_pool = await create_pool()
    await init_db()
    me = await bot.get_me()
    BOT_USERNAME = me.username or "bot"
    await bot.set_my_commands([BotCommand(command="start", description="🚀 Запустить бота")])
    if ADMIN_ID:
        try:
            await bot.set_my_commands(
                [BotCommand(command="start", description="🚀 Запустить бота"),
                 BotCommand(command="admin", description="🛠 Админка"),
                 BotCommand(command="clearcache", description="🗑 Очистить кэш")],
                scope=BotCommandScopeChat(chat_id=ADMIN_ID))
        except Exception: pass
    asyncio.create_task(start_health_server())
    log.info("Бот @%s запущен", BOT_USERNAME)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        await db_pool.close()

if __name__ == "__main__":
    asyncio.run(main())
