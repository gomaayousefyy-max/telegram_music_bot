
import asyncio
import concurrent.futures
import logging
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls.exceptions import NoActiveGroupCall
from pytgcalls.filters import chat_update, stream_end
from pytgcalls.types import ChatUpdate, StreamEnded
from pytgcalls.types.stream import AudioQuality, MediaStream
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from yt_dlp import YoutubeDL

from config import Config

# ============================================================
# (1) Logger
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logger = logging.getLogger("music_bot")


# ============================================================
# (2) Data class: Track
# ============================================================
@dataclass
class Track:
    """معلومات أغنية واحدة في الطابور."""

    title: str
    duration: int
    url: str
    file_path: Optional[str]  # بقى Optional عشان نقدر نضيف القوائم قبل ما نحملها
    requester_id: int
    requester_name: str


# ============================================================
# (3) State management - لكل جروب طابور وحالة مستقلة
# ============================================================
class ChatState:
    """حالة التشغيل لجروب واحد."""

    def __init__(self) -> None:
        self.queue: list[Track] = []
        self.current: Optional[Track] = None
        self.is_paused: bool = False
        self.is_playing: bool = False
        # متغيرات جديدة للأزرار وحساب الوقت
        self.now_playing_message_id: Optional[int] = None
        self.playback_start_time: float = 0.0
        self.elapsed_time_before_pause: float = 0.0

    def clear(self) -> None:
        """يمسح الطابور وكل الملفات اللي اتحملت."""
        if self.current and os.path.exists(self.current.file_path):
            try:
                os.remove(self.current.file_path)
            except OSError:
                pass
        for t in self.queue:
            if os.path.exists(t.file_path):
                try:
                    os.remove(t.file_path)
                except OSError:
                    pass
        self.queue.clear()
        self.current = None
        self.is_paused = False
        self.is_playing = False
        # تصفير متغيرات الأزرار والوقت عشان البوت يبدأ من جديد نضيف
        self.now_playing_message_id = None
        self.playback_start_time = 0.0
        self.elapsed_time_before_pause = 0.0


_states: dict[int, ChatState] = defaultdict(ChatState)
_locks: dict[int, asyncio.Lock] = {}

# Executor مخصص لتحميل الأغانيات عشان ميزحمش باقي البوت
_download_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=3, thread_name_prefix="ytdl"
)


# مرجع عام للبوت عشان نقدر نبعت رسايل من خارج الـ handlers
_bot_ref = None


def get_lock(chat_id: int) -> asyncio.Lock:
    """يرجّع قفل مستقل لكل جروب."""
    if chat_id not in _locks:
        _locks[chat_id] = asyncio.Lock()
    return _locks[chat_id]


def get_state(chat_id: int) -> ChatState:
    return _states[chat_id]


# ============================================================
# (4) Clients
# ============================================================
user_client = Client(
    name="music_bot_user",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    session_string=Config.SESSION_STRING,
    in_memory=True,
)

calls = PyTgCalls(user_client, cache_duration=100)


# ============================================================
# (5) Helpers
# ============================================================
URL_RE = re.compile(
    r"^(https?://)?(www\.)?(youtube\.com|youtu\.be|m\.youtube\.com)/.+$",
    re.IGNORECASE,
)


def is_url(text: str) -> bool:
    return bool(URL_RE.match(text.strip()))


def fmt_duration(seconds: int) -> str:
    if seconds <= 0:
        return "00:00"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def fmt_user(user) -> str:
    if not user:
        return "Unknown"
    name = user.first_name or ""
    if user.last_name:
        name += f" {user.last_name}"
    return name.strip() or f"ID:{user.id}"


async def bot_send(chat_id: int, text: str) -> None:
    """يبعت رسالة للجروب عبر البوت (من أي مكان في الكود)."""
    global _bot_ref
    if _bot_ref:
        try:
            await _bot_ref.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.warning("Failed to send message to %s: %s", chat_id, e)


def get_player_buttons(state: ChatState) -> InlineKeyboardMarkup:
    """يبني أزرار التحكم في الأغنية (تقديم/تأخير/إيقاف/تخطي)."""
    # زرار الإيقاف/التشغيل بيتغير حسب حالة الأغنية
    pause_resume_btn = InlineKeyboardButton(
        "⏸️ إيقاف" if state.is_playing else "▶️ تشغيل",
        callback_data="player_pause_resume"
    )
    
    buttons = [
        [
            InlineKeyboardButton("⏪ -10s", callback_data="player_seek_back"),
            InlineKeyboardButton("⏩ +10s", callback_data="player_seek_fwd"),
        ],
        [
            pause_resume_btn,
            InlineKeyboardButton("⏭️ تخطي", callback_data="player_skip"),
            InlineKeyboardButton("⏹️ إنهاء", callback_data="player_stop"),
        ],
        [
            InlineKeyboardButton("❌ إغلاق", callback_data="player_close")
        ]
    ]
    return InlineKeyboardMarkup(buttons)

# ============================================================
# (6) YouTube download (yt-dlp)
# ============================================================
def _ydl_opts() -> dict:
    cookie_file = "youtube.com_cookies.txt"
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": os.path.join(Config.DOWNLOAD_DIR, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "extract_flat": False,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    
    # التأكد من وجود ملف الكوكيز وطباعة رسالة في السجل للتأكيد
    if os.path.exists(cookie_file):
        opts["cookiefile"] = cookie_file
        logger.info("✅ YouTube Cookies file loaded successfully.")
    else:
        logger.warning("⚠️ YouTube Cookies file NOT FOUND! Bot may fail to download.")
        
    return opts

def _download_single(url: str) -> dict:
    """تحميل أغنية واحدة من رابط (للاستخدام الداخلي)."""
    with YoutubeDL(_ydl_opts()) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        if not os.path.exists(filename):
            base, _ = os.path.splitext(filename)
            for ext in (".m4a", ".webm", ".opus", ".mp3", ".mp4", ".mkv"):
                candidate = base + ext
                if os.path.exists(candidate):
                    filename = candidate
                    break
        return {
            "title": info.get("title", "Unknown"),
            "duration": int(info.get("duration") or 0),
            "url": info.get("webpage_url") or info.get("original_url", ""),
            "file_path": filename,
        }

def search_and_download(query: str) -> list[dict]:
    """دالة sync: بتبحث في يوتيوب وتجيب النتائج (تحمل أول واحدة بس فوراً)."""
    if is_url(query):
        target = query.strip()
    else:
        target = f"ytsearch1:{query.strip()}"

    with YoutubeDL(_ydl_opts()) as ydl:
        # download=False عشان ميسمش السيرفر يحمل قائمة كاملة مرة واحدة
        info = ydl.extract_info(target, download=False)

        if isinstance(info, dict) and "entries" in info:
            entries = [e for e in info["entries"] if e is not None]
        else:
            entries = [info]

        if not entries:
            raise ValueError("مفيش نتائج لبحثك.")

        results = []
        for i, entry in enumerate(entries):
            file_path = None
            # تحميل أول أغنية فقط فوراً عشان تشتغل على طول
            if i == 0:
                downloaded = _download_single(entry.get("webpage_url") or entry.get("original_url"))
                file_path = downloaded["file_path"]

        results.append({
            "title": entry.get("title", "Unknown"),
            "duration": int(entry.get("duration") or 0),
            "url": entry.get("webpage_url") or entry.get("original_url", ""),
            "file_path": file_path,
        })
        return results


async def download_async(query: str) -> list[dict]:
    """غلاف async حول yt-dlp (لأنه sync)."""
    loop = asyncio.get_running_loop()
    # استخدام executor مخصص عشان ميزحمش باقي العمليات
    return await loop.run_in_executor(_download_executor, search_and_download, query)


# ============================================================
# (7) Playback control
# ============================================================
async def _start_playback(chat_id: int, track: Track, start_time: int = 0) -> None:
    """يشغّل الأغنية فعلياً في المكالمة الصوتية مع دعم التقديم (Seek)."""
    # فحص إن الملف موجود ومش فاضي قبل التشغيل
    if not os.path.exists(track.file_path):
        raise FileNotFoundError(f"الملف مش موجود: {track.file_path}")
    if os.path.getsize(track.file_path) < 10_000:  # أقل من 10KB = غالباً فاسد
        raise ValueError(f"الملف فاسد أو فاضي: {track.file_path}")

    # إعداد FFmpeg: إذا كان فيه تقديم (Seek) نستخدم -ss
    seek_param = f"-ss {start_time} " if start_time > 0 else ""
    ffmpeg_params = f"{seek_param}-re -nostdin -threads 0"

    stream = MediaStream(
        track.file_path,
        audio_parameters=AudioQuality.STUDIO,
        ffmpeg_parameters=ffmpeg_params
    )
    await calls.play(chat_id, stream)

async def _preload_track(track: Track) -> None:
    """يحمّل ملف الأغنية في الخلفية قبل ما دورها يجي."""
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: None)  # placeholder
        # الملف بيتحمّل وقت الـ download_async أصلاً،
        # بس لو حابب تسوي نظام preload حقيقي محتاج تغير بنية Track
        logger.info("Preloaded: %s", track.title)
    except Exception as e:
        logger.warning("Preload failed for %s: %s", track.title, e)


async def play_next(chat_id: int) -> None:
    """يشغّل الأغنية اللي بعدها في الطابور."""
    async with get_lock(chat_id):
        state = get_state(chat_id)

        # تنظيف ملف الأغنية اللي خلصت
        if state.current and os.path.exists(state.current.file_path):
            try:
                os.remove(state.current.file_path)
            except OSError:
                pass
            state.current = None

        if not state.queue:
            state.is_playing = False
            state.is_paused = False
            await bot_send(
                chat_id,
                "🔚 الطابور خلص.\n"
                "اكتب /play <اسم أغنية أو رابط> عشان نشغل حاجة تانية.",
            )
            return

        track = state.queue.pop(0)
        state.current = track
        state.is_playing = True
        state.is_paused = False

    # التشغيل الفعلي بره القفل
    try:
        # لو الأغنية من قائمة تشغيل (file_path = None) نحملها الأول
        if not track.file_path or not os.path.exists(track.file_path):
            await bot_send(chat_id, f"⏳ جاري تحميل: {track.title} ...")
            loop = asyncio.get_running_loop()
            downloaded = await loop.run_in_executor(_download_executor, _download_single, track.url)
            track.file_path = downloaded["file_path"]
        
        await _start_playback(chat_id, track)
        
        # إرسال الملصق المتحرك إذا كان موجوداً
        if Config.NOW_PLAYING_STICKER:
            try:
                await _bot_ref.send_sticker(chat_id, Config.NOW_PLAYING_STICKER)
            except Exception as e:
                logger.warning("Failed to send sticker: %s", e)
        
        # إرسال رسالة الأغنية مع الأزرار التفاعلية
        text_msg = (
            f"▶️ **دلوقتي بتشتغل:**\n"
            f"🎵 {track.title}\n"
            f"⏱️ {fmt_duration(track.duration)}\n"
            f"👤 طلبها: {track.requester_name}"
        )
        msg = await _bot_ref.send_message(
            chat_id=chat_id,
            text=text_msg,
            reply_markup=get_player_buttons(state)
        )
        
        # حفظ بيانات الرسالة ووقت التشغيل
        async with get_lock(chat_id):
            state.now_playing_message_id = msg.message_id
            state.playback_start_time = time.time()
            state.elapsed_time_before_pause = 0.0
    except NoActiveGroupCall:
        
        async with get_lock(chat_id):
            state.current = None
            state.is_playing = False
            state.is_paused = False
            state.queue.insert(0, track)
        await bot_send(
            chat_id,
            "⚠️ مفيش مكالمة صوتية شغالة دلوقتي.\n"
            "افتح Voice Chat في الجروب الأول وبعدين اكتب /play تاني.",
        )
    except Exception as e:
        logger.exception("Error in play_next for chat %s", chat_id)
        await bot_send(
            chat_id,
            f"⚠️ الأغنية دي فيها مشكلة ({type(e).__name__}).\n"
            f"⏭️ هننتقل للأغنية اللي بعدها تلقائياً...",
        )
        # مسح الأغنية الفاسدة من الـ state
        async with get_lock(chat_id):
            state.current = None
            state.is_playing = False
            state.is_paused = False
        # تنظيف الملف
        if track and os.path.exists(track.file_path):
            try:
                os.remove(track.file_path)
            except OSError:
                pass
        # محاولة تشغيل اللي بعدها بعد ثانية
        await asyncio.sleep(1)
        asyncio.create_task(play_next(chat_id))


# ============================================================
# (8) pytgcalls event handlers (API الجديد: on_update + filters)
# ============================================================
@calls.on_update(stream_end())
async def on_stream_end(_, update: StreamEnded) -> None:
    """لما الأغنية تخلص تلقائياً -> نشغل اللي بعدها."""
    chat_id = update.chat_id
    logger.info("Stream ended in chat %s", chat_id)
    asyncio.create_task(play_next(chat_id))


@calls.on_update(chat_update(ChatUpdate.Status.CLOSED_VOICE_CHAT))
async def on_closed_voice_chat(_, update: ChatUpdate) -> None:
    """لما المكالمة الصوتية تقفل -> نمسح الطابور."""
    chat_id = update.chat_id
    logger.info("Voice chat closed in %s", chat_id)
    state = get_state(chat_id)
    state.clear()
    await bot_send(chat_id, "👋 المكالمة الصوتية اتقفلت. الطابور اتمسح.")


# ============================================================
# (9) PTB command handlers
# ============================================================


# --- /play | /تشغيل ---
async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text(
            "❌ اكتب اسم الأغنية أو رابط يوتيوب.\n"
            "مثال:\n"
            "  /play حماده هلال\n"
            "  /play https://youtu.be/xxxxx"
        )
        return

    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("❌ اكتب اسم الأغنية أو رابط يوتيوب.")
        return

    user_name = fmt_user(update.effective_user)
    status = await update.message.reply_text(f"🔍 بدور على: {query} ...")

    try:
        info_list = await download_async(query)
    except Exception as e:
        await status.edit_text(
            f"❌ مقدرتش ألاقي أو أحمل الأغنية.\nالسبب: {type(e).__name__}"
        )
        return

    # تم إلغاء فحص المدة نهائياً عشان يشغل أي حاجة (سور، محاضرات)

    tracks_to_add = []
    for info in info_list:
        tracks_to_add.append(
            Track(
                title=info["title"],
                duration=info["duration"],
                url=info["url"],
                file_path=info["file_path"],
                requester_id=update.effective_user.id,
                requester_name=user_name,
            )
        )

    async with get_lock(chat_id):
        state = get_state(chat_id)

        # لو فيه حاجة شغالة -> نضيف كل القائمة للطابور
        if state.is_playing or state.is_paused:
            if len(state.queue) + len(tracks_to_add) > Config.MAX_QUEUE:
                await status.edit_text(
                    f"❌ الطابور مليان ({Config.MAX_QUEUE} أغنية). استنى شوية."
                )
                return
            state.queue.extend(tracks_to_add)
            await status.edit_text(
                f"➕ اتضافت {len(tracks_to_add)} أغنية للطابور."
            )
            return

        # مفيش حاجة شغالة -> نضيف الباقي للطابور ونشغل أول واحدة
        state.current = tracks_to_add[0]
        state.is_playing = True
        state.is_paused = False
        if len(tracks_to_add) > 1:
            state.queue.extend(tracks_to_add[1:])

    track = state.current
    # التشغيل الفعلي (بره القفل)
    try:
        await _start_playback(chat_id, track)
        
        # إرسال الملصق المتحرك
        if Config.NOW_PLAYING_STICKER:
            try:
                await _bot_ref.send_sticker(chat_id, Config.NOW_PLAYING_STICKER)
            except Exception:
                pass

        # إرسال رسالة الأغنية مع الأزرار
        msg = await _bot_ref.send_message(
            chat_id=chat_id,
            text=(
                f"▶️ **بدأت تشغيل:**\n"
                f"🎵 {track.title}\n"
                f"⏱️ {fmt_duration(track.duration)}\n"
                f"👤 طلبها: {track.requester_name}"
            ),
            reply_markup=get_player_buttons(state)
        )
        await status.delete() # مسح رسالة "بدور على"
        
        # حفظ بيانات الرسالة ووقت التشغيل
        async with get_lock(chat_id):
            state.now_playing_message_id = msg.message_id
            state.playback_start_time = time.time()
            state.elapsed_time_before_pause = 0.0
    except NoActiveGroupCall:
        
        async with get_lock(chat_id):
            state.current = None
            state.is_playing = False
            state.is_paused = False
            state.queue.insert(0, track)
        await status.edit_text(
            "⚠️ مفيش مكالمة صوتية شغالة في الجروب.\n"
            "افتح Voice Chat في الجروب الأول وبعدين اكتب /play تاني."
        )
    except Exception as e:
        logger.exception("Error in play_command for chat %s", chat_id)
        await status.edit_text(f"❌ حصل خطأ: {type(e).__name__}")


# --- /pause | /وقف ---
async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    if not state.is_playing:
        await update.message.reply_text("⚠️ مفيش حاجة شغالة دلوقتي عشان نوقفها.")
        return
    try:
        await calls.pause(chat_id)
        state.is_paused = True
        state.is_playing = False
        await update.message.reply_text(
            "⏸️ الأغنية اتوقفت مؤقتاً. اكتب /resume عشان تكمل."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ {type(e).__name__}")


# --- /resume | /كمل ---
async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    if not state.is_paused:
        await update.message.reply_text("⚠️ الأغنية مش متوقفة مؤقتاً.")
        return
    try:
        await calls.resume(chat_id)
        state.is_paused = False
        state.is_playing = True
        await update.message.reply_text("▶️ كملنا التشغيل.")
    except Exception as e:
        await update.message.reply_text(f"❌ {type(e).__name__}")


# --- /skip | /تخطي ---
async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    if not state.current:
        await update.message.reply_text("⚠️ مفيش أغنية شغالة.")
        return
    # في الـ API الجديد، play() بتبدل الـ stream لو فيه call شغال
    # فهنشغل اللي بعدها على طول (play هتستبدل الحالية)
    await update.message.reply_text("⏭️ نقلنا للأغنية اللي بعدها.")
    asyncio.create_task(play_next(chat_id))


# --- /stop | /ايقاف ---
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    state.clear()
    try:
        await calls.leave_call(chat_id)
    except Exception:
        pass
    await update.message.reply_text("⏹️ وقفنا التشغيل ومسحنا الطابور.")


# --- /leave | /خروج ---
async def leave_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    state.clear()
    try:
        await calls.leave_call(chat_id)
        await update.message.reply_text("👋 طلعت من المكالمة الصوتية.")
    except Exception as e:
        await update.message.reply_text(f"❌ {type(e).__name__}")


# --- /queue | /قائمة ---
async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)

    if not state.current and not state.queue:
        await update.message.reply_text("📭 الطابور فاضي.")
        return

    lines = ["📋 **الطابور:**\n"]
    if state.current:
        marker = "⏸️ (متوقفة)" if state.is_paused else "▶️ (شغالة)"
        lines.append(
            f"**1.** {state.current.title}\n"
            f"    {fmt_duration(state.current.duration)} | "
            f"{state.current.requester_name} {marker}\n"
        )
    for i, t in enumerate(state.queue, start=2):
        lines.append(
            f"**{i}.** {t.title}\n"
            f"    {fmt_duration(t.duration)} | {t.requester_name}\n"
        )
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n... (الطابور طويل)"
    await update.message.reply_text(text)


# --- /volume | /صوت ---
async def volume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    if not state.current:
        await update.message.reply_text("⚠️ مفيش أغنية شغالة.")
        return
    if not context.args:
        await update.message.reply_text(
            "🔊 اكتب /volume 50 للتغيير (الرقم بين 1 و 200)."
        )
        return
    try:
        vol = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ الصوت لازم يكون رقم (1-200).")
        return
    if not 1 <= vol <= 200:
        await update.message.reply_text("❌ الصوت لازم يكون بين 1 و 200.")
        return
    try:
        await calls.change_volume_call(chat_id, vol)
        await update.message.reply_text(f"🔊 الصوت دلوقتي: {vol}%")
    except AttributeError:
        await update.message.reply_text(
            "⚠️ تغيير الصوت أثناء التشغيل مش مدعوم في إصدار pytgcalls ده.\n"
            "حدّد DEFAULT_VOLUME في .env بدل ده."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ {type(e).__name__}")


# --- /ping | /بنج ---
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    start = time.time()
    msg = await update.message.reply_text("🏓 بونج...")
    elapsed_ms = int((time.time() - start) * 1000)
    await msg.edit_text(f"🏓 بونج! {elapsed_ms}ms")


# --- /help | /مساعدة ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        f"🎵 **{Config.BOT_NAME}** — v{Config.BOT_VERSION}\n\n"
        "**الأوامر:**\n"
        "▶️ /play <اسم أو رابط> — شغّل أغنية\n"
        "⏸️ /pause — وقف مؤقت\n"
        "▶️ /resume — كمل\n"
        "⏭️ /skip — الأغنية اللي بعدها\n"
        "⏹️ /stop — وقّف كل حاجة وامسح الطابور\n"
        "📋 /queue — شوف الطابور\n"
        "🔊 /volume <1-200> — غيّر الصوت\n"
        "👋 /leave — اطلع من المكالمة\n"
        "🏓 /ping — فحص السرعة\n"
        "❓ /help — الرسالة دي\n\n"
        "**أوامر بالعربي:** /تشغيل /وقف /كمل /تخطي /ايقاف /قائمة /صوت /خروج /مساعدة\n\n"
        f"أقصى مدة للأغنية: {fmt_duration(Config.MAX_DURATION)}\n"
        f"أقصى حجم للطابور: {Config.MAX_QUEUE} أغنية\n\n"
        "_ملاحظة: لازم يكون فيه Voice Chat شغال في الجروب قبل /play._"
    )
    await update.message.reply_text(text)


# ============================================================
# (9.5) Inline Buttons Handler (التحكم بالأزرار)
# ============================================================
async def player_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """بيتعامل مع ضغطات الأزرار (إيقاف، تشغيل، تخطي، تقديم، تأخير)."""
    query = update.callback_query
    await query.answer() # رد فوري على تيليجرام إننا استلمنا الضغطة
    
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    data = query.data

    if data == "player_close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if not state.current:
        await query.edit_message_text("⚠️ مفيش أغنية شغالة دلوقتي.")
        return

    # زرار الإيقاف/التشغيل
    if data == "player_pause_resume":
        try:
            if state.is_playing:
                # حساب الوقت اللي فات قبل الإيقاف
                state.elapsed_time_before_pause += time.time() - state.playback_start_time
                await calls.pause(chat_id)
                state.is_playing = False
                state.is_paused = True
            else:
                await calls.resume(chat_id)
                state.is_playing = True
                state.is_paused = False
                state.playback_start_time = time.time()
            
            await query.edit_message_reply_markup(reply_markup=get_player_buttons(state))
        except Exception as e:
            logger.warning("Pause/Resume error: %s", e)

    # زرار تخطي
    elif data == "player_skip":
        await query.edit_message_text("⏭️ جاري التخطي...")
        asyncio.create_task(play_next(chat_id))

    # زرار إنهاء
    elif data == "player_stop":
        state.clear()
        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass
        await query.edit_message_text("⏹️ تم إيقاف التشغيل ومسح الطابور.")

    # زرار التقديم +10s
    elif data == "player_seek_fwd":
        current_elapsed = state.elapsed_time_before_pause + (time.time() - state.playback_start_time if state.is_playing else 0)
        new_time = int(current_elapsed) + 10
        if new_time < state.current.duration:
            await query.edit_message_text("⏩ جاري التقديم 10 ثواني...")
            await _start_playback(chat_id, state.current, start_time=new_time)
            state.playback_start_time = time.time()
            state.elapsed_time_before_pause = new_time
            state.is_playing = True
            state.is_paused = False
            await query.edit_message_reply_markup(reply_markup=get_player_buttons(state))
        else:
            # تم تعديلها لتجنب خطأ Telegram BadRequest (لأن query.answer اتعملت فوق)
            await query.edit_message_text("⚠️ وصلنا لنهاية الأغنية.")

    # زرار التأخير -10s
    elif data == "player_seek_back":
        current_elapsed = state.elapsed_time_before_pause + (time.time() - state.playback_start_time if state.is_playing else 0)
        new_time = max(0, int(current_elapsed) - 10)
        await query.edit_message_text("⏪ جاري التأخير 10 ثواني...")
        await _start_playback(chat_id, state.current, start_time=new_time)
        state.playback_start_time = time.time()
        state.elapsed_time_before_pause = new_time
        state.is_playing = True
        state.is_paused = False
        await query.edit_message_reply_markup(reply_markup=get_player_buttons(state))


# ============================================================
# (10) Startup / Shutdown
# ============================================================
async def post_init(application: Application) -> None:
    """بيتشغل قبل ما البوت يبدأ polling.
    هنا بنشغل الـ user_client و pytgcalls."""
    global _bot_ref

    Config.validate()
    Config.ensure_dirs()

    logger.info("==========================================")
    logger.info("  Starting %s v%s", Config.BOT_NAME, Config.BOT_VERSION)
    logger.info("==========================================")

    # تشغيل Pyrofork user client
    await user_client.start()
    logger.info("✅ User client started.")

    # تشغيل pytgcalls
    await calls.start()
    logger.info("✅ PyTgCalls started.")

    # حفظ مرجع البوت عشان نستخدمه من خارج الـ handlers
    _bot_ref = application.bot

    me = await application.bot.get_me()
    logger.info("✅ Bot is alive as @%s (ID: %s)", me.username, me.id)
    logger.info("------------------------------------------")
    logger.info("البوت جاهز. ابعت /help في الجروب اللي فيه Voice Chat.")
    logger.info("------------------------------------------")


async def post_stop(application: Application) -> None:
    """بيتشغل لما البوت يقفل. تنظيف."""
    try:
        await calls.stop()
    except Exception:
        pass
    try:
        await user_client.stop()
    except Exception:
        pass
    # قفل executor التحميلات
    _download_executor.shutdown(wait=False)
    logger.info("Bot stopped.")
