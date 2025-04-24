# backend/Dockerfile
FROM python:3.11-slim-bullseye
ENV DEBIAN_FRONTEND=noninteractive

# ── system packages ─────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc supervisor git net-tools libnss3 tzdata \
    && rm -rf /var/lib/apt/lists/*

# timezone
RUN ln -fs /usr/share/zoneinfo/Asia/Kuala_Lumpur /etc/localtime \
    && dpkg-reconfigure -f noninteractive tzdata

# noVNC + websockify
RUN git clone https://github.com/novnc/noVNC       /opt/novnc \
 && git clone https://github.com/novnc/websockify  /opt/novnc/utils/websockify

WORKDIR /app

# ── python deps ─────────────────────────────────────────────────
COPY backend/requirements.txt .
RUN python -m pip install --upgrade pip setuptools wheel \
 && pip install --no-cache-dir playwright \
 && playwright install --with-deps \
 && pip install --no-cache-dir -r requirements.txt

# ── source code ─────────────────────────────────────────────────
COPY backend/app                   ./app
COPY backend/run_browser_task.py   .
COPY backend/entrypoint.sh         /entrypoint.sh
COPY backend/supervisord.conf      /etc/supervisor/conf.d/supervisord.conf
# **NEW** – copy the real SPA (needed for image-only builds)
COPY backend/app/frontend          ./frontend

RUN chmod +x /entrypoint.sh && mkdir -p /app/tasks

EXPOSE 8000 6080 5901
ENV DISPLAY=:99
ENTRYPOINT ["/entrypoint.sh"]
