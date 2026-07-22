#!/bin/bash
# ============================================================
#  start.sh
#  بيشغّل سيرفر توليد PO Token في الخلفية، وبعدها البوت الأساسي
# ============================================================
set -e

echo "🔑 Starting PO Token provider..."
node /app/pot-provider/server/build/main.js &

# ننتظر شوية عشان السيرفر يبقى جاهز قبل ما البوت يبدأ يطلب منه
sleep 4

echo "🎵 Starting main bot..."
python main.py
