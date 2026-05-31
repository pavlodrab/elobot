FROM python:3.11-slim

# tesseract + Russian + English language data are needed for the
# match-screenshot OCR (see ocr.py).
# fonts-dejavu-core is needed by standings_image.py for rendering the
# /table standings table as a PNG with Cyrillic glyphs.
# libraqm0 + libfribidi0 enable HarfBuzz/Raqm text shaping inside Pillow
# (loaded via dlopen at runtime). Without them subdivision-flag emojis
# like 🏴󠁧󠁢󠁥󠁮󠁧󠁿 (England) / 🏴󠁧󠁢󠁳󠁣󠁴󠁿 (Scotland) / 🏴󠁧󠁢󠁷󠁬󠁳󠁿 (Wales) render
# as a plain black flag because GSUB tag-sequence substitutions in
# NotoColorEmoji aren't applied.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-rus \
        tesseract-ocr-eng \
        fonts-dejavu-core \
        libraqm0 \
        libfribidi0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default SQLite location for local Docker runs. Railway / Postgres ignores
# this — it sets DATABASE_URL and the bot uses Postgres instead.
# (Persistent storage for SQLite is configured at the orchestrator level:
#  docker-compose.yml mounts a named volume; on Railway use a Railway Volume
#  attached to /data, not a Dockerfile VOLUME directive.)
ENV DB_PATH=/data/league.db

CMD ["python", "bot.py"]
