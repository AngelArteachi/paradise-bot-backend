FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
# `extra_ca` is optional: it lets the build download packages behind a
# proxy or antivirus that inspects HTTPS. See docker-compose.override.example.yml.
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install --no-cache-dir -r requirements.txt

COPY backend/ .
RUN useradd --create-home app && mkdir -p /data && chown app /data
USER app

ENV DATABASE_PATH=/data/db.sqlite3 MEDIA_ROOT=/data/media PORT=8000
EXPOSE 8000
# One process: the web chat's rate limits are kept in its memory. Threads serve concurrent visitors.
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn core.wsgi --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 90"]
