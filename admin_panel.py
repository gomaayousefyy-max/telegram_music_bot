import os
import sys
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

# نجيب الـ ID بتاع الأدمن من متغيرات البيئة (هتضيفه في Railway)
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

async def shutdown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # نتأكد إن اللي بيكتب هو الأدمن بس
    if update.effective_user.id != ADMIN_ID:
        return
    
    await update.message.reply_text("تمام يا باشا، البوت هيتقفل دلوقتي... 👋")
    
    # ده هيقفل الـ Python process بالكامل في أي مكان هو شغال فيه
    sys.exit(0)

# دالة عشان نوصلها في الملف الرئيسي
def setup_admin_handlers(application):
    application.add_handler(CommandHandler("shutdown", shutdown_command))
