# One image definition, one image per component (select with EXTRAS).
#   docker build -f deploy/docker/Dockerfile --build-arg EXTRAS=orchestrator -t orchestrator .
#   EXTRAS: orchestrator | agent | token-service | mock | domain | api
# Dependencies are pinned by requirements/<EXTRAS>.lock when it exists (make lock).
FROM python:3.12-slim AS build
ARG EXTRAS=orchestrator
WORKDIR /build
COPY pyproject.toml README.md ./
COPY requirements ./requirements
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip \
 && if [ -f "requirements/${EXTRAS}.lock" ]; then set -- -c "requirements/${EXTRAS}.lock"; fi \
 && pip wheel --no-cache-dir --wheel-dir /wheels "$@" ".[${EXTRAS}]"

FROM python:3.12-slim
ARG EXTRAS=orchestrator
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HOME=/app/.cache/huggingface
RUN groupadd --system app && useradd --system --gid app --uid 10001 --home-dir /app app
WORKDIR /app
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels
# The voice agent's models (VAD, turn detector) are baked in so pods start offline.
RUN if [ "$EXTRAS" = "agent" ]; then python -m livekit.agents download-files && chmod -R a+rX /app/.cache; fi
ENV HF_HUB_OFFLINE=1
COPY config ./config
COPY policies ./policies
USER 10001
EXPOSE 8080
# Default: orchestrator API. Override the command for worker / agents / token service.
CMD ["python", "-m", "orchestrator"]
