FROM python:3.12-slim

ARG GIT_REVISION=unknown
LABEL org.opencontainers.image.title="free-model-checker" \
      org.opencontainers.image.description="Watches OpenRouter and OpenCode free-model inventories and alerts a Discord webhook." \
      org.opencontainers.image.source="https://github.com/markbrown87/free-model-checker" \
      org.opencontainers.image.url="https://github.com/markbrown87/free-model-checker" \
      org.opencontainers.image.revision="${GIT_REVISION}"

# Unbuffered stdout/stderr so log lines appear immediately in `docker compose logs -f`.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Application code and the default (non-secret) config
COPY --chown=1000:1000 checker.py config.toml ./
# Baked-in default templates. The operator may bind-mount ./templates over these;
# when the mount is absent these copies are used.
COPY --chown=1000:1000 templates/ ./templates/
# Materialise a uid/gid-1000-owned /app/data (the stand-in file is just a way to
# create an empty, writable directory via COPY) so the container can persist
# state even when the ./data bind mount is absent.
COPY --chown=1000:1000 data/.gitkeep ./data/.gitkeep

# Run as a non-root user with uid/gid 1000
USER 1000:1000

CMD ["python", "checker.py"]
