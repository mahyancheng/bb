# backend/Dockerfile
# Use a specific slim Python version for reproducibility
FROM python:3.11-slim-bullseye
ENV DEBIAN_FRONTEND=noninteractive

# Install OS dependencies: xvfb, vnc, supervisor, git, net-tools, timezone data, common libs
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc supervisor git net-tools libnss3 tzdata \
    && rm -rf /var/lib/apt/lists/*

# Set Timezone (example: Kuala Lumpur)
RUN ln -fs /usr/share/zoneinfo/Asia/Kuala_Lumpur /etc/localtime \
    && dpkg-reconfigure -f noninteractive tzdata

# Clone noVNC and websockify for the VNC web interface
RUN git clone https://github.com/novnc/noVNC       /opt/novnc && \
    git clone https://github.com/novnc/websockify  /opt/novnc/utils/websockify

# Set working directory
WORKDIR /app

# Install Python dependencies using requirements.txt
# Copy only requirements first to leverage Docker layer caching
COPY backend/requirements.txt .
# Upgrade pip and install Playwright first (which has complex dependencies)
RUN python -m pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir playwright && \
    # Install Playwright browsers and OS dependencies
    playwright install --with-deps && \
    # Install remaining requirements
    pip install --no-cache-dir -r requirements.txt

# Copy the rest of the backend application code
COPY backend/app                   ./app
COPY backend/run_browser_task.py   .
COPY backend/entrypoint.sh         /entrypoint.sh
COPY backend/supervisord.conf      /etc/supervisor/conf.d/supervisord.conf
# Copy the frontend files into the image (will be overridden by volume mount if docker-compose is used)
COPY backend/frontend              ./frontend

# Ensure entrypoint is executable and task directory exists
RUN chmod +x /entrypoint.sh && mkdir -p /app/tasks

# Expose necessary ports
EXPOSE 8000 6080 5901

# Set environment variable for display used by Xvfb/X11
ENV DISPLAY=:99

# Set entrypoint script
ENTRYPOINT ["/entrypoint.sh"]