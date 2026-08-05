import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler
from config import Config
from music_helpers import force_leave_other_chats, get_active_chats_info

logger = logging.getLogger("music_bot.admin")

async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """هاندلر أمر /admin - بيفتح لوحة التحكم للأدمن بس."""
    if update.effective_user.id not in Config.ADMIN_IDS:
        return
        
    text = await get_active_chats_info()
    
    keyboard = [
        [InlineKeyboardButton("🔌 افصل البوت من كل الجروبات", callback_data="admin_disconnect")],
        [InlineKeyboardButton("🔄 تحديث الحالة", callback_data="admin_refresh")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(text, reply_markup=reply_markup)

async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """هاندلر أزرار لوحة الأدمن."""
    query = update.callback_query
    
    # حماية: لو المشهور مش الأدمن، نرفض ونموت
    if query.from_user.id not in Config.ADMIN_IDS:
        await query.answer("🚫 مينفعش، دي لوحة تحكم الأدمن بس.", show_alert=True)
        return
        
    await query.answer()
    data = query.data
    
    if data == "admin_disconnect":
        # 0 هنا معناها افصل البوت من كل الجروبات (مش هتعزل جروب بعينه)
        await force_leave_other_chats(exclude_chat_id=0)
        await query.edit_message_text(
            "✅ تم فصل البوت من كل الجروبات بنجاح.\nتقدر تشغله دلوقتي في أي مكان براحتك.",
            reply_markup=None
        )
    elif data == "admin_refresh":
        text = await get_active_chats_info()
        keyboard = [
            [InlineKeyboardButton("🔌 افصل البوت من كل الجروبات", callback_data="admin_disconnect")],
            [InlineKeyboardButton("🔄 تحديث الحالة", callback_data="admin_refresh")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

def setup_admin_handlers(application) -> None:
    """تسجيل هاندلرات الأدمن في البوت."""
    application.add_handler(CommandHandler("admin", admin_panel_command))
    application.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^admin_"))
