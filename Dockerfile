# ============================================================
#  music_bot/Dockerfile
#  بناء صريح يضمن تثبيت Python + Node + ffmpeg + سيرفر PO Token
# ============================================================

FROM python:3.11-slim

# تثبيت الأدوات الأساسية للنظام: ffmpeg للصوت، git وnode لسيرفر التوكن
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    unzip \
    ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# تثبيت Deno - مطلوب من yt-dlp لفك تشفير سيغنتشر يوتيوب (nsig/EJS)
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y

WORKDIR /app

# تثبيت مكتبات بايثون
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --upgrade --force-reinstall git+https://github.com/yt-dlp/yt-dlp.git \
    && pip install --no-cache-dir --upgrade yt-dlp-ejs

# تجهيز سيرفر PO Token (Node.js)
RUN git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /app/pot-provider \
    && cd /app/pot-provider/server \
    && npm install \
    && npx tsc

    # تثبيت NTP لتزامن الوقت
RUN apt-get update && apt-get install -y ntp ntpdate && \
    ntpdate pool.ntp.org && \
    systemctl enable ntp || true

# نسخ باقي كود البوت
COPY . .

RUN chmod +x start.sh

CMD ["bash", "start.sh"]
