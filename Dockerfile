FROM python:3.12-slim

# Unbuffered stdout/stderr so log lines appear immediately in `docker compose logs -f`.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Application code and the default (non-secret) config. There are zero third-party
# dependencies, so there is deliberately no `pip install` step.
COPY checker.py config.toml ./
# Baked-in default templates. The operator may bind-mount ./templates over these;
# when the mount is absent these copies are used.
COPY templates/ ./templates/

# Run as a non-root user with uid/gid 1000. The host operator is typically uid 1000,
# so bind-mounted ./data and ./templates stay writable/readable without chown on the host.
RUN groupadd --gid 1000 checker \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin checker \
    && mkdir -p /app/data \
    && chown -R checker:checker /app

USER checker

CMD ["python", "checker.py"]
