#!/bin/bash
ntpdate -u pool.ntp.org 2>/dev/null || true
# ... باقي الأوامر اللي موجودة (تشغيل سيرفر PO Token ثم main.py)
set -e

echo "🔑 Starting PO Token provider..."
node /app/pot-provider/server/build/main.js &

# ننتظر شوية عشان السيرفر يبقى جاهز قبل ما البوت يبدأ يطلب منه
sleep 4

echo "🎵 Starting main bot..."
python main.py
