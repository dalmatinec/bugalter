FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY migrations ./migrations
COPY bugalter ./bugalter

RUN useradd --system --uid 10001 app
USER app

# Миграции применяются при каждом старте (идемпотентно), затем запускается бот.
CMD ["sh", "-c", "alembic upgrade head && python -m bugalter"]
