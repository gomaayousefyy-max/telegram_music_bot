# ============================================================
#  music_bot/config.py
#  ملف الإعدادات المركزي لبوت الأغاني
#  بيقرأ كل القيم من ملف .env اللي جنب الملف ده
#  ممنوع تكتب أي قيمة حساسة هنا - كلها في .env
# ============================================================

import os
from dotenv import load_dotenv

# تحميل ملف .env من نفس فولدر الملف
load_dotenv()


class Config:
    """كل إعدادات البوت مجموعة في كلاس واحد."""

    # ----------------------------------------------------------
    # (1) بيانات تيليجرام - من https://my.telegram.org
    # ----------------------------------------------------------
    API_ID: int = int(os.getenv("API_ID", "0"))
    API_HASH: str = os.getenv("API_HASH", "")

    # توكن بوت جديد منفصل من @BotFather
    # لازم مختلف عن بتاع البوت الأساسي عشان مفيش تعارض polling
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

    # جلسة اليوزر اللي هتدخل الكول وتشغل الصوت (Pyrogram session string)
    # بتتولّد عبر سكريبت هنكمله في خطوة قادمة
    SESSION_STRING: str = os.getenv("SESSION_STRING", "")

    # ----------------------------------------------------------
    # (2) Supabase - تكامل اختياري مع البوت الأساسي
    #     لو مش هتستخدمه سيبهم فاضيين
    # ----------------------------------------------------------
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

    # ----------------------------------------------------------
    # (3) إعدادات التشغيل
    # ----------------------------------------------------------
    # جودة التحميل من يوتيوب
    # bestaudio/best تضمن سحب الصوت من أي فيديو حتى لو مفيش مسار صوت مستقل
    YDL_FORMAT: str = os.getenv("YDL_FORMAT", "bestaudio/best")
    # مستوى الصوت الافتراضي (0 - 200)  | 100 = طبيعي
    DEFAULT_VOLUME: int = int(os.getenv("DEFAULT_VOLUME", "100"))

    # تم إلغاء قيد المدة نهائياً عشان يشغل أي حاجة (سور، محاضرات، قوائم طويلة)
    MAX_DURATION: int = int(os.getenv("MAX_DURATION", "99999"))
    # أقصى عدد أغانين في الطابور
    MAX_QUEUE: int = int(os.getenv("MAX_QUEUE", "30"))

    # فولدر تخزين الأغانيات المؤقتة
    DOWNLOAD_DIR: str = os.getenv("DOWNLOAD_DIR", "downloads")

    # مسار ffmpeg (لو مش في الـ PATH اكتب المسار الكامل زي /usr/bin/ffmpeg)
    FFMPEG_PATH: str = os.getenv("FFMPEG_PATH", "ffmpeg")

    # ----------------------------------------------------------
    # (4) هوية البوت
    # ----------------------------------------------------------
    BOT_NAME: str = os.getenv("BOT_NAME", "🎵 شيخ الجروب - بوت الأغاني")
    BOT_VERSION: str = "1.0.0"
    
    # معرف الملصق المتحرك (Sticker ID) اللي هيتبعت قبل رسالة الأغنية
    # هتجيب الـ ID ده من تيليجرام وتحطه في متغير البيانات NOW_PLAYING_STICKER
    NOW_PLAYING_STICKER: str = os.getenv("NOW_PLAYING_STICKER", "")

    # ----------------------------------------------------------
    # (5) صلاحيات الأدمن (معرفات تيليجرام مفصولة بفواصل)
    #     عشان يقدروا يوقفوا/يخطفوا/يمسحوا الطابور
    # ----------------------------------------------------------
    ADMIN_IDS: list[int] = [
        int(x.strip())
        for x in os.getenv("ADMIN_IDS", "").split(",")
        if x.strip().isdigit()
    ]

    # ----------------------------------------------------------
    # فولدر التحميلات - نتأكد إنه موجود من بدري
    # ----------------------------------------------------------
    @classmethod
    def ensure_dirs(cls) -> None:
        """يتأكد إن فولدر التحميلات موجود، ويعمله لو مش موجود."""
        os.makedirs(cls.DOWNLOAD_DIR, exist_ok=True)

    # ----------------------------------------------------------
    # فحص الإعدادات الإلزامية قبل تشغيل البوت
    # (الاستدعاء ده بيتعمل من main.py في الخطوة الجاية)
    # ----------------------------------------------------------
    @classmethod
    def validate(cls) -> None:
        """يتأكد إن كل القيم الإلزامية موجودة.
        لو فيه حاجة ناقصة بيرمي رسالة واضحة ويوقف التشغيل."""
        missing: list[str] = []

        if cls.API_ID == 0:
            missing.append("API_ID")
        if not cls.API_HASH:
            missing.append("API_HASH")
        if not cls.BOT_TOKEN:
            missing.append("BOT_TOKEN")
        if not cls.SESSION_STRING:
            missing.append("SESSION_STRING")

        if missing:
            raise RuntimeError(
                "❌ فيه متغيرات ناقصة في ملف .env:\n"
                + "\n".join(f"   - {m}" for m in missing)
                + "\n\n ملء البيانات دي إلزامي قبل تشغيل البوت."
            )


# لما الملف يتـimport، نتأكد بس إن فولدر التحميلات موجود
# التحقق الكامل بيحصل في main.py عشان رسالة الخطأ تظهر واضحة وقت التشغيل
Config.ensure_dirs()
