FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user. /data is a bind-mounted volume (./data:/data) that
# holds the SQLite DB and art cache, so its on-disk ownership comes from the
# HOST, not the image. UID/GID 1000 is used (rather than an arbitrary system
# UID) because it matches the conventional first non-root Linux user account,
# i.e. the owner of ./data on a typical single-user deploy host. The host
# ./data directory must be owned by (or group-writable to) UID/GID 1000 for
# the container to write commander.db and art/ through the mount; chown the
# host directory to match if it is owned by a different user.
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid app --home-dir /app --no-create-home app \
    && mkdir -p /data \
    && chown -R app:app /app /data

USER app

# GUNICORN_WORKER_TIMEOUT_SECS: generous timeout for card-art/deck-import
# requests that proxy to Scryfall/Moxfield/Archidekt.
CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "4", "--timeout", "60", "app:app"]
