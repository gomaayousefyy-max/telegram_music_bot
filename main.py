# ============================================================
#  music_bot/main.py
#  ملف التسجيل الأساسي - بيجمع كل المميزات من music_helpers
# ============================================================

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from config import Config

# استيراد كل الدوال والـ Clients من ملف الإضافات
from music_helpers import (
    post_init,
    post_stop,
    play_command,
    pause_command,
    resume_command,
    skip_command,
    stop_command,
    leave_command,
    queue_command,
    volume_command,
    ping_command,
    help_command,
    player_callback_handler,
    global_error_handler,
)

# ============================================================
# Main entry point
# ============================================================
def main() -> None:
    application = (
        Application.builder()
        .token(Config.BOT_TOKEN)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )
    
    application.add_error_handler(global_error_handler)

    # تسجيل الأوامر (عربي + إنجليزي)

    # أوامر إنجليزي بس (Telegram مابيقبلش عربي في CommandHandler)
    application.add_handler(CommandHandler(["play", "p"], play_command))
    application.add_handler(CommandHandler(["pause", "pau"], pause_command))
    application.add_handler(CommandHandler(["resume", "r"], resume_command))
    application.add_handler(CommandHandler(["skip", "next", "s"], skip_command))
    application.add_handler(CommandHandler(["stop", "end"], stop_command))
    application.add_handler(CommandHandler(["leave", "l"], leave_command))
    application.add_handler(CommandHandler(["queue", "q", "list"], queue_command))
    application.add_handler(CommandHandler(["volume", "v"], volume_command))
    application.add_handler(CommandHandler(["ping"], ping_command))
    application.add_handler(CommandHandler(["help", "h"], help_command))
    
    # تسجيل الـ Handler بتاع الأزرار التفاعلية
    application.add_handler(CallbackQueryHandler(player_callback_handler, pattern="^player_"))

    # أوامر عربي عبر MessageHandler (لأن PTB مابيقبلش عربي في CommandHandler)
    arabic_commands = {
        "تشغيل": play_command,
        "شغل": play_command,
        "وقف": pause_command,
        "كمل": resume_command,
        "استمرار": resume_command,
        "تخطي": skip_command,
        "التالي": skip_command,
        "ايقاف": stop_command,
        "خروج": leave_command,
        "اطلع": leave_command,
        "قائمة": queue_command,
        "الطابور": queue_command,
        "صوت": volume_command,
        "الصوت": volume_command,
        "بنج": ping_command,
        "مساعدة": help_command,
    }

    async def arabic_command_router(
        update: Update, context
    ) -> None:
        """يوزع الأوامر العربية على الدوال المناسبة."""
        if not update.message or not update.message.text:
            return
        text = update.message.text.strip()
        # نشيل الـ / أو ! أو . من بداية الأمر لو موجودة
        if text[:1] in ("/", "!", "."):
            text = text[1:].strip()
        # ناخد أول كلمة بس
        first_word = text.split()[0] if text.split() else ""
        handler = arabic_commands.get(first_word)
        if handler:
            # نمرر args لو موجودة (لـ /play و /volume)
            args = text.split()[1:] if len(text.split()) > 1 else []
            context.args = args
            await handler(update, context)

    # فلتر: أي رسالة نصية تبدأ بـ / أو ! أو . أو نص عربي لوحده
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & filters.Regex(
                r"^(/|!|\.|)(تشغيل|شغل|وقف|كمل|استمرار|تخطي|التالي|ايقاف|خروج|اطلع|قائمة|الطابور|صوت|الصوت|بنج|مساعدة)\b"
            ),
            arabic_command_router,
        )
    )

    # تشغيل البوت
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
