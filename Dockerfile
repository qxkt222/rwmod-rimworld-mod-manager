# ============================================================
# rwmod — Self-contained RimWorld Mod Manager
# Multi-stage build: Python backend + Vite frontend + SteamCMD
# ============================================================

# ── Stage 1: Frontend build (Node) ──────────────────────────
FROM node:22-alpine AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package.json frontend/bun.lock* ./
RUN npm install
COPY frontend/ ./
RUN npx vite build --outDir /app/static

# ── Stage 2: Python dependencies ────────────────────────────
FROM python:3.13-slim AS python-deps
WORKDIR /app
# Dependencies are derived from pyproject.toml — the single source of truth —
# instead of being re-listed by hand. The previous hand-written list had
# silently drifted and omitted `defusedxml`, which made the image die at
# startup with ModuleNotFoundError. Adding a dependency must never again
# require a matching edit here.
COPY pyproject.toml ./
RUN DEPS=$(python -c 'import tomllib;print(" ".join(tomllib.load(open("pyproject.toml","rb"))["project"]["dependencies"]))') \
    && pip install --no-cache-dir --prefix=/install $DEPS

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
