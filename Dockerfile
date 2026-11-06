# Container image for the Cloud Run demo (TASKS.md 6.3).
#
# Built early, ahead of Phase 6, so auth/env/container problems surface now
# rather than with a day left. The app it serves is still the Phase 0.5
# skeleton — /health plus a token-gated /report that 501s. Real endpoints get
# wired in as later phases land; the deployment path does not change with them.
#
# Two stages so the runtime image carries no build tooling. Dependencies are
# installed from the committed lockfile before any source is copied, so editing
# src/ does not re-resolve or re-download the dependency tree.

# --- build stage -----------------------------------------------------------
# Base images pinned by digest (tag: python:3.11-slim); Dependabot keeps them current.
FROM python:3.11-slim@sha256:65bd3d9f1d7fa268dd1909973ba7eb4bdf41f6dcf2cfbe76ba9528b170f3fd79 AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Pinned to the version that generated uv.lock — an unpinned installer is a
# silent way for the build to stop matching the lockfile it claims to honour.
RUN pip install --no-cache-dir uv==0.11.25

WORKDIR /app
COPY pyproject.toml uv.lock ./

# --frozen: fail if uv.lock disagrees with pyproject, never silently re-resolve.
# --no-install-project: the app is run from source on PYTHONPATH rather than
# installed as a wheel, so a source edit needs no reinstall step.
RUN uv sync --frozen --no-dev --no-install-project

# --- runtime stage ---------------------------------------------------------
FROM python:3.11-slim@sha256:65bd3d9f1d7fa268dd1909973ba7eb4bdf41f6dcf2cfbe76ba9528b170f3fd79 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/app/.venv/bin:$PATH" \
    PORT=8080

# Non-root by default. Cloud Run does not require it; running as root anyway in
# a container that will hold five live API keys is a needless default to accept.
RUN useradd --create-home --uid 1000 groundwork
WORKDIR /app

COPY --from=build --chown=groundwork:groundwork /app/.venv /app/.venv
COPY --chown=groundwork:groundwork src ./src

USER groundwork
EXPOSE 8080

# Shell form on purpose: Cloud Run injects $PORT at runtime and exec form would
# pass it through as a literal string. `exec` keeps uvicorn as PID 1 so it
# receives SIGTERM directly and shuts down cleanly on container replacement.
CMD exec uvicorn src.api.main:app --host 0.0.0.0 --port $PORT
