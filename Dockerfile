# Образ для хостингов, где удобнее Docker (Render, Amvera, HF Spaces, VPS).
# Локально Docker не проверялся — при первом деплое смотреть логи сборки.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Сначала зависимости — слой кэшируется между сборками
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# На хостинге с постоянным томом: -v /data:/data и DATA_DIR=/data
ENV DATA_DIR=/app/data

# Порт HTTP-эндпоинта для «живости» сервиса (0 — выключен).
# Render передаёт свой PORT автоматически; для HF Spaces нужен 7860.
ENV PORT=0

CMD ["python", "bot.py"]
