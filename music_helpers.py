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
from pyrogram.errors import AuthKeyDuplicated
from pytgcalls import PyTgCalls
from pytgcalls.exceptions import NoActiveGroupCall
from pytgcalls.filters import chat_update, stream_end
from pytgcalls.types import ChatUpdate, StreamEnded
from pytgcalls.types.stream import AudioQuality, MediaStream
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from config import Config

MUSIC_GIF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "now_playing.gif")
_cached_gif_file_id: Optional[str] = None
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
    file_path: Optional[str]
    requester_id: int
    requester_name: str

# ============================================================
# (3) State management
# ============================================================
class ChatState:
    """حالة التشغيل لجروب واحد."""
    def __init__(self) -> None:
        self.queue: list[Track] = []
        self.current: Optional[Track] = None
        self.is_paused: bool = False
        self.is_playing: bool = False
        self.now_playing_message_id: Optional[int] = None
        self.playback_start_time: float = 0.0
        self.elapsed_time_before_pause: float = 0.0

    def clear(self) -> None:
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
        self.now_playing_message_id = None
        self.playback_start_time = 0.0
        self.elapsed_time_before_pause = 0.0

_states: dict[int, ChatState] = defaultdict(ChatState)
_locks: dict[int, asyncio.Lock] = {}

_download_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=3, thread_name_prefix="ytdl"
)

_bot_ref = None

def get_lock(chat_id: int) -> asyncio.Lock:
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
    r"^(https?://)?(www.)?(youtube.com|youtu.be|m.youtube.com)/.+$",
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
    global _bot_ref
    if _bot_ref:
        try:
            await _bot_ref.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.warning("Failed to send message to %s: %s", chat_id, e)

def get_player_buttons(state: ChatState) -> InlineKeyboardMarkup:
    pause_text = "𝒫a̲u̲s̲e̲ إيقاف" if state.is_playing else "▶️ تشغيل"
    buttons = [
        [
            InlineKeyboardButton(pause_text, callback_data="player_pause_resume"),
            InlineKeyboardButton("🔇 إنهاء", callback_data="player_stop"),
            InlineKeyboardButton("𝒮𝓀𝒾𝓅 تخطي", callback_data="player_skip"),
        ],
        [
            InlineKeyboardButton("⟲ -10s", callback_data="player_seek_back"),
            InlineKeyboardButton("▶️", callback_data="player_pause_resume"),
            InlineKeyboardButton("⟳ +10s", callback_data="player_seek_fwd"),
        ],
        [
            InlineKeyboardButton("🛡️ S̲u̲p̲p̲o̲r̲t̲ ↗️", url="https://t.me/your_channel"),
        ],
        [
            InlineKeyboardButton("💳 A̲D̲D̲ T̲O̲ G̲R̲O̲U̲P̲ ↗️", url="https://t.me/your_bot?startgroup=true"),
        ]
    ]
    return InlineKeyboardMarkup(buttons)
# ============================================================
# (6) YouTube download (yt-dlp) - تم تنظيف كل المسافات هنا
# ============================================================
def _ydl_opts() -> dict:
    cookie_file = "youtube.com_cookies.txt"
    opts = {
        "format": Config.YDL_FORMAT,
        "outtmpl": os.path.join(Config.DOWNLOAD_DIR, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "extract_flat": False,
        "socket_timeout": 30,
        "retries": 10,
        "fragment_retries": 10,
        "continuedl": True,
        "concurrent_fragment_downloads": 4,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "opus",
                "preferredquality": "192",
            }
        ],
        "extractor_args": {
            "youtube": {
                "player_client": ["tv", "android", "web"],
            },
            "youtubepot-bgutilhttp": {
                "base_url": [os.getenv("POT_PROVIDER_URL", "http://127.0.0.1:4416")],
            },
        },
    }
    if os.path.exists(cookie_file):
        opts["cookiefile"] = cookie_file
        logger.info("✅ YouTube Cookies file loaded successfully.")
    else:
        logger.warning("⚠️ YouTube Cookies file NOT FOUND! Bot may fail to download.")
    return opts

def _download_single(url: str) -> dict:
    """تحميل أغنية واحدة من رابط (للاستخدام الداخلي)."""
    try:
        with YoutubeDL(_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
    except DownloadError as e:
        logger.warning(f"Download failed with primary format: {e}")
        fallback_opts = _ydl_opts().copy()
        fallback_opts['format'] = 'bestaudio/best'
        with YoutubeDL(fallback_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

    if not os.path.exists(filename):
        
        base, _ = os.path.splitext(filename)
        for ext in (".m4a", ".webm", ".opus", ".mp3", ".mp4", ".mkv"):
            candidate = base + ext
            if os.path.exists(candidate):
                filename = candidate
                break
    duration = int(info.get("duration") or 0)

    if os.path.exists(filename):
        size_mb = os.path.getsize(filename) / (1024 * 1024)
        # فحص تقريبي: أقل من 0.05 ميجا لكل دقيقة معناه الملف ناقص/فاسد
        expected_min_mb = (duration / 60) * 0.05
        if duration > 60 and size_mb < expected_min_mb:
            logger.warning(
                "⚠️ الملف ناقص محتمل: %s (%.2f MB لمدة %s ثانية)",
                filename, size_mb, duration,
            )
    return {
        "title": info.get("title", "Unknown"),
        "duration": duration,
        "url": info.get("webpage_url") or info.get("original_url", ""),
        "file_path": filename,
    }


def _finish_search(info: dict) -> list[dict]:
    if isinstance(info, dict) and "entries" in info:
        entries = [e for e in info["entries"] if e is not None]
    else:
        entries = [info]

    results = []
    for entry in entries:
        url = entry.get("webpage_url") or entry.get("url") or entry.get("original_url")
        video_id = entry.get("id")

        # فحص الكاش من غير أي اتصال إضافي بيوتيوب - نستخدم الـ id الجاهز من البحث
        cached_path = None
        if video_id:
            for ext in (".opus", ".m4a", ".webm", ".mp3", ".mp4", ".mkv"):
                candidate = os.path.join(Config.DOWNLOAD_DIR, f"{video_id}{ext}")
                if os.path.exists(candidate) and os.path.getsize(candidate) > 10_000:
                    cached_path = candidate
                    break

        if cached_path:
            logger.info("⚡ استخدام نسخة مخزنة (كاش) بدل التحميل: %s", cached_path)
            results.append({
                "title": entry.get("title", "Unknown"),
                "duration": int(entry.get("duration") or 0),
                "url": entry.get("webpage_url") or entry.get("original_url", ""),
                "file_path": cached_path,
            })
        else:
            downloaded = _download_single(url)
            results.append(downloaded)
    return results

    
def search_and_download(query: str) -> list[dict]:
    if is_url(query):
        target = query.strip()
        sources = [target]
    else:
        # نحاول يوتيوب الأول، ولو فشل نجرب SoundCloud كبديل
        sources = [f"ytsearch1:{query.strip()}", f"scsearch1:{query.strip()}"]

    last_error = None
    for target in sources:
        try:
            with YoutubeDL(_ydl_opts()) as ydl:
                info = ydl.extract_info(target, download=False)
            if info:
                if target.startswith("scsearch"):
                    logger.info("🔄 اتحمل من SoundCloud بعد فشل يوتيوب: %s", query)
                return _finish_search(info)
        except DownloadError as e:
            last_error = e
            logger.warning(f"Search failed on source '{target[:12]}...': {e}")
            try:
                fallback_opts = _ydl_opts().copy()
                fallback_opts['format'] = 'best'
                with YoutubeDL(fallback_opts) as ydl:
                    info = ydl.extract_info(target, download=False)
                if info:
                    return _finish_search(info)
            except DownloadError as e2:
                last_error = e2
                continue

    raise last_error or DownloadError("فشل التحميل من كل المصادر المتاحة.")

async def download_async(query: str) -> list[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_download_executor, search_and_download, query)

# ============================================================
# (7) Playback control
# ============================================================
async def _start_playback(chat_id: int, track: Track, start_time: int = 0) -> None:
    if not os.path.exists(track.file_path):
        raise FileNotFoundError(f"الملف مش موجود: {track.file_path}")
    if os.path.getsize(track.file_path) < 10_000:
        raise ValueError(f"الملف فاسد أو فاضي: {track.file_path}")
    
    # Pre-resolve peer عشان نحل KeyError: ID not found (session cache miss)
    try:
        await user_client.resolve_peer(chat_id)
    except AuthKeyDuplicated:
        raise
    except Exception as e:
        logger.warning("resolve_peer فشل لـ %s: %s", chat_id, type(e).__name__)
    
    seek_param = f"-ss {start_time} " if start_time > 0 else ""
    ffmpeg_params = f"{seek_param}-nostdin -threads 0 -fflags +genpts+igndts -avoid_negative_ts make_zero"
    
    stream = MediaStream(
        track.file_path,
        audio_parameters=AudioQuality.STUDIO,
        ffmpeg_parameters=ffmpeg_params
    )
    try:
        await calls.play(chat_id, stream)
    except AuthKeyDuplicated:
        # مفيش فايدة نعمل retry - فيه نسخة تانية شغالة بنفس الـ session
        logger.error(
            "AuthKeyDuplicated أثناء التشغيل في chat %s - فيه نسخة تانية شغالة بنفس SESSION_STRING.",
            chat_id,
        )
        raise
    except Exception as e:
        logger.warning("فشلت أول محاولة تشغيل (%s)، بننضف الاتصال ونعيد المحاولة...", type(e).__name__)
        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass
        await asyncio.sleep(3)
        try:
            await calls.play(chat_id, stream)
        except AuthKeyDuplicated:
            logger.error(
                "AuthKeyDuplicated في إعادة المحاولة لـ chat %s - فيه نسخة تانية شغالة بنفس SESSION_STRING.",
                chat_id,
            )
            raise
async def play_next(chat_id: int) -> None:
    async with get_lock(chat_id):
        state = get_state(chat_id)
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
                "🔚 الطابور خلص.\nاكتب /play <اسم أغنية أو رابط> عشان نشغل حاجة تانية.",
            )
            return
            
        track = state.queue.pop(0)
        state.current = track
        state.is_playing = True
        state.is_paused = False

    try:
        if not track.file_path or not os.path.exists(track.file_path):
            await bot_send(chat_id, f"⏳ جاري تحميل: {track.title} ...")
            loop = asyncio.get_running_loop()
            downloaded = await loop.run_in_executor(_download_executor, _download_single, track.url)
            track.file_path = downloaded["file_path"]
            
        await _start_playback(chat_id, track)
        
        if hasattr(Config, 'NOW_PLAYING_STICKER') and Config.NOW_PLAYING_STICKER:
            try:
                await _bot_ref.send_sticker(chat_id, Config.NOW_PLAYING_STICKER)
            except Exception as e:
                logger.warning("Failed to send sticker: %s", e)
                
        text_msg = (
            f"🎙️ - تم تشغيل: {state.current.title} 🎶\n"
            f"🔊 - مدة التشغيل #{fmt_duration(state.current.duration)}"
        )
        try:
            global _cached_gif_file_id
            msg = await _bot_ref.send_animation(
                chat_id=chat_id,
                animation=_cached_gif_file_id or open(MUSIC_GIF_PATH, "rb"),
                caption=text_msg,
                reply_markup=get_player_buttons(state)
            )
            if not _cached_gif_file_id and msg.animation:
                _cached_gif_file_id = msg.animation.file_id
        except Exception as e:
            logger.warning("Failed to send GIF, falling back to text: %s", e)
            msg = await _bot_ref.send_message(
                chat_id=chat_id,
                text=text_msg,
                reply_markup=get_player_buttons(state)
            )
        
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
            "⚠️ مفيش مكالمة صوتية شغالة دلوقتي.\nافتح Voice Chat في الجروب الأول وبعدين اكتب /play تاني.",
        )
    except Exception as e:
        logger.exception("Error in play_next for chat %s", chat_id)
        await bot_send(
            chat_id,
            f"⚠️ الأغنية دي فيها مشكلة ({type(e).__name__}).\n⏭️ هننتقل للأغنية اللي بعدها تلقائياً...",
        )
        async with get_lock(chat_id):
            state.current = None
            state.is_playing = False
            state.is_paused = False
        if track and os.path.exists(track.file_path):
            try:
                os.remove(track.file_path)
            except OSError:
                pass
        await asyncio.sleep(1)
        asyncio.create_task(play_next(chat_id))

# ============================================================
# (8) pytgcalls event handlers
# ============================================================
@calls.on_update(stream_end())
async def on_stream_end(_, update: StreamEnded) -> None:
    chat_id = update.chat_id
    logger.info("Stream ended in chat %s", chat_id)
    asyncio.create_task(play_next(chat_id))

@calls.on_update(chat_update(ChatUpdate.Status.CLOSED_VOICE_CHAT))
async def on_closed_voice_chat(_, update: ChatUpdate) -> None:
    chat_id = update.chat_id
    logger.info("Voice chat closed in %s", chat_id)
    state = get_state(chat_id)
    state.clear()
    await bot_send(chat_id, "👋 المكالمة الصوتية اتقفلت. الطابور اتمسح.")

# ============================================================
# (9) PTB command handlers
# ============================================================
async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("❌ اكتب اسم الأغنية أو رابط يوتيوب.\nمثال:\n  /play حماده هلال\n  /play https://youtu.be/xxxxx")
        return
    
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("❌ اكتب اسم الأغنية أو رابط يوتيوب.")
        return
        
    user_name = fmt_user(update.effective_user)
    status = await update.effective_chat.send_message(f"🔍 بدور على: {query} ...")
    
    try:
        info_list = await download_async(query)
    except Exception as e:
        await status.edit_text(f"❌ مقدرتش ألاقي أو أحمل الأغنية.\nالسبب: {type(e).__name__}")
        return
        
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
        if state.is_playing or state.is_paused:
            if len(state.queue) + len(tracks_to_add) > Config.MAX_QUEUE:
                await status.edit_text(f"❌ الطابور مليان ({Config.MAX_QUEUE} أغنية). استنى شوية.")
                return
            state.queue.extend(tracks_to_add)
            await status.edit_text(f"➕ اتضافت {len(tracks_to_add)} أغنية للطابور.")
            return
            
        state.current = tracks_to_add[0]
        state.is_playing = True
        state.is_paused = False
        if len(tracks_to_add) > 1:
            state.queue.extend(tracks_to_add[1:])
            
    track = state.current
    try:
        await _start_playback(chat_id, track)
        if hasattr(Config, 'NOW_PLAYING_STICKER') and Config.NOW_PLAYING_STICKER:
            try:
                await _bot_ref.send_sticker(chat_id, Config.NOW_PLAYING_STICKER)
            except Exception:
                pass
                
        text_msg = (
            f"🎙️ - تم تشغيل: {state.current.title} 🎶\n"
            f"🔊 - مدة التشغيل #{fmt_duration(state.current.duration)}"
        )
        try:
            global _cached_gif_file_id
            msg = await _bot_ref.send_animation(
                chat_id=chat_id,
                animation=_cached_gif_file_id or open(MUSIC_GIF_PATH, "rb"),
                caption=text_msg,
                reply_markup=get_player_buttons(state)
            )
            if not _cached_gif_file_id and msg.animation:
                _cached_gif_file_id = msg.animation.file_id
        except Exception as e:
            logger.warning("Failed to send GIF, falling back to text: %s", e)
            msg = await _bot_ref.send_message(
                chat_id=chat_id,
                text=text_msg,
                reply_markup=get_player_buttons(state)
            )
        await status.delete()
        
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
        await status.edit_text("⚠️ مفيش مكالمة صوتية شغالة في الجروب.\nافتح Voice Chat في الجروب الأول وبعدين اكتب /play تاني.")
    except Exception as e:
        async with get_lock(chat_id):
            state.current = None
            state.is_playing = False
            state.is_paused = False
            state.queue.insert(0, track)
        logger.exception("Error in play_command for chat %s", chat_id)
        err_str = str(e)
        if "AUTH_KEY_DUPLICATED" in err_str or "AuthKeyDuplicated" in err_str:
            await status.edit_text(
                "🚫 الجلسة (SESSION_STRING) مستخدمة في أكتر من مكان في نفس الوقت.\n\n"
                "الأسباب المحتملة:\n"
                "  1) فيه Deployment تاني شغال على Railway بنفس الجلسة (امسحه وسيب واحد بس Active).\n"
                "  2) Replicas أكتر من 1 في Settings (خليها 1).\n"
                "  3) شغال نسخة محلية على جهازك بنفس SESSION_STRING وقت ما Railway شغال.\n\n"
                "انتظر دقيقة وبعدين جرب /play تاني."
            )
        elif "CHAT_ADMIN_REQUIRED" in err_str or "ChatAdminRequired" in err_str:
            await status.edit_text(
                "🚫 الحساب اللي بيشغل الصوت لازم يبقى أدمن في الجروب.\n"
                "روح إعدادات الجروب → الأعضاء → حط الحساب أدمن وفعّل صلاحية "
                "\"إدارة المكالمات الصوتية\"، وبعدين اكتب /play تاني."
            )
        else:
            await status.edit_text(f"❌ حصل خطأ: {type(e).__name__}\nجرب /play تاني، ولو استمرت المشكلة قوللي.")
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
        await update.message.reply_text("⏸️ الأغنية اتوقفت مؤقتاً. اكتب /resume عشان تكمل.")
    except Exception as e:
        await update.message.reply_text(f"❌ {type(e).__name__}")

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

async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    if not state.current:
        await update.message.reply_text("⚠️ مفيش أغنية شغالة.")
        return
    await update.message.reply_text("⏭️ نقلنا للأغنية اللي بعدها.")
    asyncio.create_task(play_next(chat_id))

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    state.clear()
    try:
        await calls.leave_call(chat_id)
    except Exception:
        pass
    await update.message.reply_text("⏹️ وقفنا التشغيل ومسحنا الطابور.")

async def leave_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    state.clear()
    try:
        await calls.leave_call(chat_id)
        await update.message.reply_text("👋 طلعت من المكالمة الصوتية.")
    except Exception as e:
        await update.message.reply_text(f"❌ {type(e).__name__}")

async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    if not state.current and not state.queue:
        await update.message.reply_text("📭 الطابور فاضي.")
        return
        
    lines = ["📋 **الطابور:**\n"]
    if state.current:
        marker = "⏸️ (متوقفة)" if state.is_paused else "▶️ (شغالة)"
        lines.append(f"**1.** {state.current.title}\n    {fmt_duration(state.current.duration)} | {state.current.requester_name} {marker}\n")
        
    for i, t in enumerate(state.queue, start=2):
        lines.append(f"**{i}.** {t.title}\n    {fmt_duration(t.duration)} | {t.requester_name}\n")
        
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n... (الطابور طويل)"
    await update.message.reply_text(text)

async def volume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    if not state.current:
        await update.message.reply_text("⚠️ مفيش أغنية شغالة.")
        return
    if not context.args:
        await update.message.reply_text("🔊 اكتب /volume 50 للتغيير (الرقم بين 1 و 200).")
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
        await update.message.reply_text("⚠️ تغيير الصوت أثناء التشغيل مش مدعوم في إصدار pytgcalls ده.\nحدّد DEFAULT_VOLUME في .env بدل ده.")
    except Exception as e:
        await update.message.reply_text(f"❌ {type(e).__name__}")

async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    start = time.time()
    msg = await update.message.reply_text("🏓 بونج...")
    elapsed_ms = int((time.time() - start) * 1000)
    await msg.edit_text(f"🏓 بونج! {elapsed_ms}ms")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        f"🎵 {Config.BOT_NAME} — v{Config.BOT_VERSION}\n\n"
        "الأوامر:\n"
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
        "أوامر بالعربي: /تشغيل /وقف /كمل /تخطي /ايقاف /قائمة /صوت /خروج /مساعدة\n\n"
        f"أقصى مدة للأغنية: {fmt_duration(Config.MAX_DURATION)}\n"
        f"أقصى حجم للطابور: {Config.MAX_QUEUE} أغنية\n\n"
        "ملاحظة: لازم يكون فيه Voice Chat شغال في الجروب قبل /play."
    )
    await update.message.reply_text(text)

# ============================================================
# (9.5) Inline Buttons Handler
# ============================================================
async def player_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
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
        await query.edit_message_caption("⚠️ مفيش أغنية شغالة دلوقتي.")
        return
        
    if data == "player_pause_resume":
        try:
            if state.is_playing:
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
            
    elif data == "player_skip":
        await query.edit_message_caption("⏭️ جاري التخطي...")
        asyncio.create_task(play_next(chat_id))
        
    elif data == "player_stop":
        state.clear()
        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass
        await query.edit_message_caption("⏹️ تم إيقاف التشغيل ومسح الطابور.")
        
    elif data == "player_seek_fwd":
        current_elapsed = state.elapsed_time_before_pause + (time.time() - state.playback_start_time if state.is_playing else 0)
        new_time = int(current_elapsed) + 10
        if new_time < state.current.duration:
            await query.edit_message_caption("⏩ جاري التقديم 10 ثواني...")
            await _start_playback(chat_id, state.current, start_time=new_time)
            state.playback_start_time = time.time()
            state.elapsed_time_before_pause = new_time
            state.is_playing = True
            state.is_paused = False
            await query.edit_message_reply_markup(reply_markup=get_player_buttons(state))
        else:
            await query.edit_message_caption("⚠️ وصلنا لنهاية الأغنية.")
            
    elif data == "player_seek_back":
        current_elapsed = state.elapsed_time_before_pause + (time.time() - state.playback_start_time if state.is_playing else 0)
        new_time = max(0, int(current_elapsed) - 10)
        await query.edit_message_caption("⏪ جاري التأخير 10 ثواني...")
        await _start_playback(chat_id, state.current, start_time=new_time)
        state.playback_start_time = time.time()
        state.elapsed_time_before_pause = new_time
        state.is_playing = True
        state.is_paused = False
        await query.edit_message_reply_markup(reply_markup=get_player_buttons(state))


import json as _json

async def web_app_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يستقبل الأزرار من صفحة الـ WebApp الملونة ويشغّل نفس منطق player_callback_handler."""
    raw = update.effective_message.web_app_data.data
    try:
        payload = _json.loads(raw)
        action = payload.get("action")
    except Exception:
        return

    chat_id = update.effective_chat.id
    state = get_state(chat_id)

    if action == "player_close":
        return
    if not state.current:
        await bot_send(chat_id, "⚠️ مفيش أغنية شغالة دلوقتي.")
        return

    if action == "player_pause_resume":
        try:
            if state.is_playing:
                state.elapsed_time_before_pause += time.time() - state.playback_start_time
                await calls.pause(chat_id)
                state.is_playing = False
                state.is_paused = True
            else:
                await calls.resume(chat_id)
                state.is_playing = True
                state.is_paused = False
                state.playback_start_time = time.time()
        except Exception as e:
            logger.warning("Pause/Resume error (webapp): %s", e)

    elif action == "player_skip":
        asyncio.create_task(play_next(chat_id))

    elif action == "player_stop":
        state.clear()
        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass
        await bot_send(chat_id, "⏹️ تم إيقاف التشغيل ومسح الطابور.")

    elif action == "player_seek_fwd":
        current_elapsed = state.elapsed_time_before_pause + (time.time() - state.playback_start_time if state.is_playing else 0)
        new_time = int(current_elapsed) + 10
        if new_time < state.current.duration:
            await _start_playback(chat_id, state.current, start_time=new_time)
            state.playback_start_time = time.time()
            state.elapsed_time_before_pause = new_time
            state.is_playing = True
            state.is_paused = False
        else:
            await bot_send(chat_id, "⚠️ وصلنا لنهاية الأغنية.")

    elif action == "player_seek_back":
        current_elapsed = state.elapsed_time_before_pause + (time.time() - state.playback_start_time if state.is_playing else 0)
        new_time = max(0, int(current_elapsed) - 10)
        await _start_playback(chat_id, state.current, start_time=new_time)
        state.playback_start_time = time.time()
        state.elapsed_time_before_pause = new_time
        state.is_playing = True
        state.is_paused = False


# ============================================================
# (10) Startup / Shutdown
# ============================================================
async def post_init(application: Application) -> None:
    global _bot_ref
    Config.validate()
    Config.ensure_dirs()
    logger.info("==========================================")
    logger.info("  Starting %s v%s", Config.BOT_NAME, Config.BOT_VERSION)
    logger.info("==========================================")
    
    max_retries = 2
    for attempt in range(1, max_retries + 1):
        try:
            await user_client.start()
            logger.info("✅ User client started.")
            break
        except Exception as e:
            if "AUTH_KEY_DUPLICATED" in str(e) or "AuthKeyDuplicated" in str(e):
                wait_time = attempt * 30
                logger.warning(
                    "⚠️ الجلسة لسه شغالة في مكان تاني (محاولة %s/%s). هستنى %s ثانية...",
                    attempt, max_retries, wait_time,
                )
                if attempt == max_retries:
                    logger.error("=" * 60)
                    logger.error("❌ فشل تشغيل user_client بعد %s محاولات.", max_retries)
                    logger.error("❌ السبب: فيه نسخة تانية من البوت شغالة بنفس SESSION_STRING.")
                    logger.error("❌ افعل الآتي:")
                    logger.error("   1) Railway → Settings → Replicas = 1")
                    logger.error("   2) Railway → Deployments → امسح أي Deployment قديم")
                    logger.error("   3) اتأكد إن مفيش بوت شغّال على جهازك بنفس الجلسة")
                    logger.error("   4) لو ولّدت SESSION_STRING من أداة online → ولّدها من سكريبت محلي بدلها")
                    logger.error("   5) وقّف البوت، استنى 5 دقايق، وشغّله تاني")
                    logger.error("=" * 60)
                    # مهم: انتظر 90 ثانية قبل ما نموت عشان تيليجرام يقفل الجلسة
                    # من جهته، وRailway لما يـ restart مش هيلاقي نفس المشكلة
                    logger.warning("⏳ هستنى 90 ثانية قبل ما أقفل عشان تيليجرام يقفل الجلسة من جهته...")
                    try:
                        await user_client.stop()
                    except Exception:
                        pass
                    await asyncio.sleep(90)
                    raise
                try:
                    await user_client.stop()
                except Exception:
                    pass
                await asyncio.sleep(wait_time)
            else:
                raise
    
    await calls.start()
    logger.info("✅ PyTgCalls started.")
    
    _bot_ref = application.bot
    me = await application.bot.get_me()
    logger.info("✅ Bot is alive as @%s (ID: %s)", me.username, me.id)
    logger.info("------------------------------------------")
    logger.info("البوت جاهز. ابعت /help في الجروب اللي فيه Voice Chat.")
    logger.info("------------------------------------------")

async def post_stop(application: Application) -> None:
    try:
        await calls.stop()
    except Exception:
        pass
    try:
        await user_client.stop()
    except Exception:
        pass
    _download_executor.shutdown(wait=False)
    logger.info("Bot stopped.")



async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يمسك أي خطأ مش متوقع في أي مكان في البوت، عشان البوت ميوقفش أبداً."""
    logger.error("Unhandled exception: %s", context.error, exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ حصلت مشكلة غير متوقعة، بس البوت لسه شغال. جرب تاني.",
            )
    except Exception:
        pass
