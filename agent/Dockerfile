# Agent service (FastAPI + LangGraph brain, arq worker) — image.
#
# Dependencies come from pyproject.toml + uv.lock, installed with `uv sync --frozen` so the
# image gets exactly the locked versions and a drifted lock fails the build instead of
# silently resolving something else. Dev dependencies (ruff/pytest/respx/fakeredis) are
# excluded — they are CI tooling, not runtime.
#
# The container runs migrations once and then supervises uvicorn + arq together; see
# docker-entrypoint.sh for why the container must die when either child does.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Install into a fixed path rather than ./.venv so the venv cannot be shadowed by a
    # bind-mounted source tree, and put it first on PATH: alembic, uvicorn and arq are
    # then callable by bare name from the entrypoint.
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # Use the interpreter this image already ships; never fetch a second one.
    UV_PYTHON_DOWNLOADS=never \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /srv

RUN pip install --no-cache-dir "uv>=0.5,<1"

# Dependencies first, as their own layer: they change far less often than app code, so a
# source-only edit does not reinstall the world.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY docker-entrypoint.sh ./

# Second sync installs the project itself now that its package is present.
RUN uv sync --frozen --no-dev \
    && chmod +x docker-entrypoint.sh

# PRIVATE port. The agent and its /internal-facing surface are service-to-service and
# shared-secret authenticated; this port must never be published to the internet.
EXPOSE 8000

RUN useradd --create-home --uid 10001 agent \
    && chown -R agent:agent /srv
USER agent

CMD ["./docker-entrypoint.sh"]
