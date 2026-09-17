FROM python:3.11.13-slim-bookworm AS base

COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /bin/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    LANGGRAPH_STRICT_MSGPACK=true \
    DEVFLOW_LLM_PROVIDER=mock

WORKDIR /app
RUN groupadd --gid 10001 devflow \
    && useradd --uid 10001 --gid devflow --no-create-home --shell /usr/sbin/nologin devflow \
    && mkdir -p /var/lib/devflow \
    && chown -R devflow:devflow /var/lib/devflow /app


FROM base AS test
COPY --chown=devflow:devflow pyproject.toml uv.lock ./
COPY --chown=devflow:devflow src ./src
COPY --chown=devflow:devflow tests ./tests
RUN uv sync --locked
USER 10001:10001
CMD ["pytest", "-q"]


FROM base AS production
COPY --chown=devflow:devflow pyproject.toml uv.lock ./
COPY --chown=devflow:devflow src ./src
RUN uv sync --locked --no-dev --no-editable
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "devflow.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]


FROM test AS runner
WORKDIR /candidate
ENTRYPOINT ["/app/.venv/bin/python", "-I", "-m", "devflow.candidate_runner"]
CMD ["lint"]


FROM docker:27.5.1-cli AS docker-cli


FROM runner AS dispatcher
USER root
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
ENTRYPOINT ["/app/.venv/bin/python", "-I", "-m", "devflow.runner_dispatch"]
CMD []

