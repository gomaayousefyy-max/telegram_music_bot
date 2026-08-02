import os
import time
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, ContextTypes, CallbackQueryHandler
import music_helpers

# نجيب الـ ID بتاع الأدمن من متغيرات البيئة
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def get_active_chat() -> int:
    """تجيب الـ chat_id بتاع الجروب اللي البوت شغال فيه دلوقتي."""
    for cid, state in music_helpers._states.items():
        if state.is_playing or state.is_paused:
            return cid
    return None

async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    
    keyboard = [
        [
            InlineKeyboardButton("🚪 افصل البوت من الجروب التاني", callback_data="admin_force_leave"),
        ],
        [
            InlineKeyboardButton("⏸️ إيقاف مؤقت", callback_data="admin_pause"),
            InlineKeyboardButton("▶️ استكمال", callback_data="admin_resume"),
            InlineKeyboardButton("⏭️ تخطي", callback_data="admin_skip"),
            InlineKeyboardButton("⏹️ إيقاف ومسح", callback_data="admin_stop"),
        ],
        [
            InlineKeyboardButton("🔴 قفل البوت (Shutdown)", callback_data="admin_shutdown"),
        ]
    ]
    await update.message.reply_text(
        "👑 **لوحة تحكم الأدمن**\n\n⚠️ زراير التحكم بتأثر على الجروب اللي البوت شغال فيه دلوقتي.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        return
    
    await query.answer()
    data = query.data
    active_chat = get_active_chat()

    if data == "admin_force_leave":
        # تفصل البوت من أي جروب تاني غير الجروب اللي الأدمن بيكتب فيه
        await music_helpers.force_leave_other_chats(update.effective_chat.id)
        await query.edit_message_text("✅ تم فصل البوت من أي جروب تاني. تقدر تشغله دلوقتي في الجروب ده.")
        return
        
    if data == "admin_shutdown":
        await query.edit_message_text("👋 البوت هيتقفل دلوقتي...")
        os._exit(0) # يقفل الـ Process نهائياً

    # لو مفيش أغنية شغالة، مفيش لازمة نكمل باقي الأزرار
    if not active_chat:
        await query.edit_message_text("⚠️ مفيش أغنية شغالة دلوقتي.")
        return

    state = music_helpers.get_state(active_chat)
    
    if data == "admin_pause" and state.is_playing:
        state.elapsed_time_before_pause += time.time() - state.playback_start_time
        await music_helpers.calls.pause(active_chat)
        state.is_playing = False
        state.is_paused = True
        await query.edit_message_text("⏸️ تم الإيقاف المؤقت في الجروب التاني.")
        
    elif data == "admin_resume" and state.is_paused:
        await music_helpers.calls.resume(active_chat)
        state.is_playing = True
        state.is_paused = False
        state.playback_start_time = time.time()
        await query.edit_message_text("▶️ تم الاستكمال في الجروب التاني.")
        
    elif data == "admin_skip":
        asyncio.create_task(music_helpers.play_next(active_chat))
        await query.edit_message_text("⏭️ تم تخطي الأغنية في الجروب التاني.")
        
    elif data == "admin_stop":
        state.clear()
        try:
            await music_helpers.calls.leave_call(active_chat)
        except Exception:
            pass
        await query.edit_message_text("⏹️ تم إيقاف التشغيل ومسح الطابور في الجروب التاني.")

def setup_admin_handlers(application):
    application.add_handler(CommandHandler("admin", admin_panel_command))
    application.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^admin_"))
