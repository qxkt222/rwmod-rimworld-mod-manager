# ============================================================
# rwmod — Self-contained RimWorld Mod Manager
# Multi-stage build: Python backend + Vite frontend + SteamCMD
# ============================================================

# ── Stage 1: Frontend build (Node) ──────────────────────────
FROM node:22-alpine AS frontend-builder
WORKDIR /app/frontend
# `npm ci` instead of `npm install`: package-lock.json is committed, so the
# build installs the exact tree the lock pins. `npm install` re-resolved every
# semver range on each build, which made two images from the same commit differ.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
# Version single source: frontend/vite.config.ts reads ../pyproject.toml
# relative to /app/frontend/vite.config.ts, i.e. /app/pyproject.toml. Without
# this file the version silently fell back to "0.0.0" in the bundled UI
# (REV-005). `.dockerignore` does not exclude pyproject.toml.
COPY pyproject.toml /app/pyproject.toml
# Fail the build instead of shipping 0.0.0 if the version cannot be read.
RUN node -e "const fs=require('fs');const m=/^version\s*=\s*\"([^\"]+)\"/m.exec(fs.readFileSync('/app/pyproject.toml','utf8'));const v=m&&m[1];if(!v||v==='0.0.0'){console.error('pyproject.toml version missing or 0.0.0');process.exit(1)}console.log('rwmod version',v)"
COPY frontend/ ./
RUN npx vite build --outDir /app/static

# ── Stage 2: Python dependencies ────────────────────────────
FROM python:3.13-slim AS python-deps
# Pin uv to the version that generated uv.lock. `uv export --frozen` converts
# the lock into a requirements file, so the image installs exactly the versions
# the local gate and CI tested. The previous `pip install <dependencies>` let
# pip resolve `>=` specifiers at build time — every build could ship different
# versions of fastapi/pydantic than the ones that passed the tests.
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-hashes --no-emit-project -o requirements.txt \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 3: Runtime ────────────────────────────────────────
FROM python:3.13-slim
WORKDIR /app

# Create non-root user
RUN groupadd -r rwmod -g 1001 && \
    useradd -r -g rwmod -u 1001 rwmod

# Home directory for the non-root user: Path.home() puts ~/.rwmod.db here,
# so it must exist and be writable by rwmod.
ENV HOME=/app/data

# Copy Python deps from builder
COPY --from=python-deps /install /usr/local

# Copy source. The package must stay at /app/src/rwmod: utils.bundle_root()
# resolves bundled static/ and steamcmd/ by walking three parents up from
# rwmod/utils.py, which lands on /app only in this exact layout. Installing the
# package into site-packages instead would make bundle_root() resolve to
# /usr/local/lib/python3.13 and the static mount would 404.
COPY pyproject.toml ./
COPY src/ ./src/
ENV PYTHONPATH=/app/src

# Copy frontend build
COPY --from=frontend-builder /app/static ./static/

# Volume mount points: built-in SteamCMD, mods, and the home/data directory.
# They must be created and chowned *before* VOLUME is declared: Docker discards
# image changes made to a path after VOLUME, which previously left the volumes
# owned by root and made sqlite / SteamCMD fail at startup.
# docker run -v ./steamcmd:/app/steamcmd -v ./mods:/app/mods ...
RUN mkdir -p /app/data /app/steamcmd /app/mods && \
    chown -R rwmod:rwmod /app/data /app/steamcmd /app/mods

# Declared after mkdir/chown on purpose — see the comment above.
VOLUME ["/app/steamcmd", "/app/mods", "/app/data"]

# Switch to non-root
USER rwmod


EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/config')" || exit 1

ENTRYPOINT ["python", "-m", "uvicorn", "rwmod.server:app", "--host", "0.0.0.0", "--port", "8000"]
