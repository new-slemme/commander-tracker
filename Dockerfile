FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

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

# 1 gthread worker with 16 threads:
#   - P3: threads handle art/static I/O without starving page renders
#   - P4: single-process memory:// Flask-Limiter is accurate (4 workers gives 4× budget)
# Flask-SQLAlchemy scoped_session and CARD_ART_CACHE_LOCK are thread-safe.
CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "1", "-k", "gthread", "--threads", "16", "--timeout", "60", "app:app"]
