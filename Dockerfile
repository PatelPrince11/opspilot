# The dashboard (frontend/) is built here; only its static files reach the app image.
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Production front door (`--target proxy`): Nginx with the built dashboard and the SSE proxy
# config. Kept before the app stage so a plain `docker build` still produces the app image.
FROM nginx:1.27-alpine AS proxy
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=web /web/dist /usr/share/nginx/html

FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so source edits don't invalidate this layer.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
COPY --from=web /web/dist frontend/dist

# The app writes only .cache (LLM response cache, Hugging Face models; prod mounts a volume
# there, which starts with this owner) and data/ (the corpus build). Chowning all of /app would
# copy the 1.4 GB venv into another layer.
RUN mkdir -p /app/.cache && useradd --create-home --uid 1000 app && chown -R app /app/.cache /app/data
USER app

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
