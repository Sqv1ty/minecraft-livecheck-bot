# ===========================================================================
#  Dockerfile для Livecheck-бота
#
#  Подходит для любого хостинга с Docker: Railway, Fly.io, VPS, Docker Hub.
#  Сборка:  docker build -t livecheck .
#  Запуск:  docker run -d --name livecheck --env-file .env livecheck
# ===========================================================================

FROM python:3.12-slim

# Не хранить pip-кэш в образе - он раздувает размер.
ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

WORKDIR /app

# Сначала зависимости - так слой с ними кешируется и пересобирается быстрее.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py palette_png.py ./

# Бот пишет настройки в .theme.json и .env - даём на это права.
RUN useradd --create-home --shell /usr/sbin/nologin bot && chown -R bot:bot /app
USER bot

# Проверка, что бот вообще стартует и видит настройки.
CMD ["python", "-u", "bot.py"]
