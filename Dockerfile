# Broadside -- single-container image (spec section 2).
#
# A slim Python base, a non-root user that owns the data volume so the config
# file's restrictive permissions are meaningful, and gunicorn serving the Flask
# app. Everything the app persists (config + post log) lives under /data, which
# is expected to be a mounted volume so it survives container restarts.

FROM python:3.12-slim

# Don't buffer stdout/stderr (so logs appear promptly) and don't write .pyc.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BROADSIDE_DATA_DIR=/data \
    BROADSIDE_PORT=8080

# Install dependencies first for better layer caching.
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code.
COPY app ./app

# Bake the source commit into the image so the app can tell whether it is behind
# the latest on GitHub (drives the in-app update banner). Passed by update.sh /
# docker compose; defaults to "unknown" for a plain local build. Placed late so
# changing it only rebuilds this tiny layer, not the pip install above.
ARG BROADSIDE_VERSION=unknown
ENV BROADSIDE_VERSION=$BROADSIDE_VERSION

# Create a non-root user and the data directory it owns. Running as non-root is
# why the 0600 config permissions actually protect the credentials on the volume.
RUN useradd --create-home --uid 10001 broadside \
    && mkdir -p /data \
    && chown -R broadside:broadside /data /srv
USER broadside

# The volume for persistent config and the post log.
VOLUME ["/data"]

EXPOSE 8080

# One worker is plenty for a single-user tool; multiple threads let a slow post
# (async Mastodon media polling) not block a second request. The long timeout
# accommodates media processing waits.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", \
     "--timeout", "180", "app.server:app"]
