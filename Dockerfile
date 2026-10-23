# Reviewhound service image: `serve` (webhook + /metrics) by default, `worker` via the command.
# No secrets are baked in: every token and password comes from the environment at run time.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uvx /bin/

# git: the worker checks out PR heads. The model runs through the `claude` CLI, which needs Node;
# build with --build-arg INSTALL_CLAUDE_CLI=1 for worker images (serve does not need it).
ARG INSTALL_CLAUDE_CLI=0
# Pinned so a rebuild never pulls an unreviewed CLI; bump it deliberately.
ARG CLAUDE_CODE_VERSION=2.1.299
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && if [ "$INSTALL_CLAUDE_CLI" = "1" ]; then \
         apt-get install -y --no-install-recommends nodejs npm \
         && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
         && npm cache clean --force; \
       fi \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    REVIEWHOUND_HOME=/data \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
# Dependencies first, so a source change does not reinstall them.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --extra postgres --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra postgres

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin reviewhound \
    && mkdir -p /data \
    && chown reviewhound:reviewhound /data
USER reviewhound
VOLUME ["/data"]
EXPOSE 8787

# Probes the webhook server; the worker service in docker-compose.yml turns this off.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["reviewhound"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8787"]
