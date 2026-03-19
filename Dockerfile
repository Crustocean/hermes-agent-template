FROM python:3.11-slim

# System deps + headless Chromium for browser automation
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl build-essential \
    chromium chromium-driver \
    fonts-liberation fonts-noto-color-emoji \
    libnss3 libatk-bridge2.0-0 libdrm2 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libasound2 libpangocairo-1.0-0 \
    libgtk-3-0 libxshmfence1 && \
    rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMIUM_FLAGS="--no-sandbox --headless --disable-gpu --disable-dev-shm-usage"

# Clone hermes-agent
RUN git clone --recurse-submodules --depth 1 \
    https://github.com/NousResearch/hermes-agent.git /app/hermes-agent

WORKDIR /app/hermes-agent

# Install hermes-agent with all extras
RUN pip install --no-cache-dir -e ".[all]" && \
    pip install --no-cache-dir -e "./mini-swe-agent" 2>/dev/null || true

# Install Crustocean adapter deps + Playwright for browser tools
RUN pip install --no-cache-dir "python-socketio[asyncio_client]" httpx playwright && \
    playwright install --with-deps chromium

# Copy Crustocean adapter and platform modules into hermes
COPY crustocean.py /app/hermes-agent/gateway/platforms/crustocean.py
COPY poker.py /app/hermes-agent/gateway/platforms/poker.py
COPY redaction.py /app/hermes-agent/gateway/platforms/redaction.py
COPY evolution.py /app/hermes-agent/gateway/platforms/evolution.py
COPY crustocean_tools.py /app/hermes-agent/tools/crustocean_tools.py

# Patch hermes-agent to register the Crustocean platform
COPY patch_hermes.py /app/patch_hermes.py
RUN python /app/patch_hermes.py /app/hermes-agent

# Hermes data lives on a Railway volume mounted at /data
ENV HERMES_HOME=/data/hermes
ENV PYTHONUNBUFFERED=1

# Copy generic defaults (overridden at runtime by config from Crustocean API)
COPY config.yaml /app/hermes-defaults/config.yaml
COPY SOUL.md /app/hermes-defaults/SOUL.md
COPY skills/ /app/hermes-defaults/skills/

# Copy startup files
COPY fetch_config.py /app/fetch_config.py
COPY start_gateway.py /app/start_gateway.py
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

CMD ["/app/start.sh"]
