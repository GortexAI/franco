FROM python:3.15-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# System deps (curl needed for the Claude CLI installer)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first (cached layer — rebuilds only when requirements change),
# then Chromium + its system libraries in the same layer cleanup pass.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

# Non-root user; owns app data and the shared browser install
RUN useradd --create-home --uid 1000 franco \
    && mkdir -p /app/data \
    && chown -R franco:franco /app /ms-playwright

# Copy project (config/prompts are typically bind-mounted over these at runtime)
COPY --chown=franco:franco franco/ franco/
COPY --chown=franco:franco prompts/ prompts/
COPY --chown=franco:franco skills/ skills/
COPY --chown=franco:franco franco.yaml .

USER franco
ENV PATH="/home/franco/.local/bin:${PATH}"

# Claude Code CLI, installed as the runtime user so ~/.local/bin is correct.
# The installer is a bash script (under sh it fails on its first array), and
# the build fails if `claude` didn't land: Franco can't think without it.
# For an offline build only: --build-arg SKIP_CLAUDE_CLI=1.
ARG SKIP_CLAUDE_CLI=0
RUN if [ "$SKIP_CLAUDE_CLI" = "1" ]; then echo "Skipping the Claude CLI (SKIP_CLAUDE_CLI=1)"; \
    else curl -fsSL https://claude.ai/install.sh | bash && claude --version; fi

# Healthy = the heartbeat loop has touched the database recently
# (2h window tolerates long heartbeat intervals and quiet-hour idling).
HEALTHCHECK --interval=5m --timeout=10s --start-period=3m --retries=3 \
    CMD python -c "import os,sys,time; p='/app/data/franco.db'; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 7200 else 1)"

CMD ["python", "-m", "franco.main"]
