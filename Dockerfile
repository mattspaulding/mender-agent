# Mender + FinPay container.
#
# Both services ship in one image; Cloud Run picks which one runs by
# overriding the entrypoint (--command on `gcloud run deploy`):
#   mender-web      — the Mender FastAPI service (default)
#   finpay-serve    — the target agent's HTTP server
#
# Image carries Python 3.12 + Node 20 alongside each other because Mender
# launches @arizeai/phoenix-mcp via `npx` over stdio. The Node toolchain
# stays in the runtime image (not just build) for that reason.
#
# Pre-warming: we npm-install @arizeai/phoenix-mcp at build time so the
# first heartbeat doesn't pay the npx fetch cost (typically 5-10s).

FROM node:20-bookworm-slim AS runtime

# --- Python toolchain ---
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# --- uv (faster than pip; matches local dev) ---
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
 && cp /root/.local/bin/uv /usr/local/bin/uv \
 && uv --version

# --- App layout ---
WORKDIR /app

# Resolve Python deps in a layer of their own so source edits don't bust the cache.
COPY pyproject.toml uv.lock ./
COPY README.md LICENSE ./
RUN uv sync --no-dev --no-install-project --frozen

# Pre-warm @arizeai/phoenix-mcp into the global npm cache so the first
# `npx -y @arizeai/phoenix-mcp@latest` in production is instant.
RUN npm install -g @arizeai/phoenix-mcp@latest \
 && npx --version \
 && which npx

# Now copy source + install our packages into the venv.
COPY src ./src
COPY prompts ./prompts
RUN uv sync --no-dev --frozen

# Cloud Run sends signals to PID 1; using uvicorn directly via the
# venv's installed entrypoint script keeps signal handling correct.
ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

# Cloud Run injects $PORT; both entrypoints honor it.
ENV PORT=8080
ENV MENDER_WEB_PORT=8080
ENV FINPAY_PORT=8080

# Default to the Mender web service. Override at deploy time:
#   gcloud run deploy finpay --image=... --command=finpay-serve
EXPOSE 8080
CMD ["mender-web"]
