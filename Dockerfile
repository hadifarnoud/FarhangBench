# FarhangBench review page (python -m farhang review) for a shared deployment; see deploy/README.md.
# The harness has no third-party dependencies, so the image is the standard library plus the repo's code and data.
FROM python:3.13.17-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FARHANG_USERS_FILE=/etc/farhang/users

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin farhang

WORKDIR /app
# Code and data only. Reviews are written to /app/validation, which the deployment mounts from a volume.
COPY --chown=root:root farhang/ farhang/
COPY --chown=root:root data/ data/
COPY --chown=root:root config/ config/
COPY --chown=root:root schema/ schema/
RUN mkdir -p /app/validation && chown farhang:farhang /app/validation

USER 10001
EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=3s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/healthz', timeout=2)"]
ENTRYPOINT ["python", "-m", "farhang", "review", "--host", "0.0.0.0", "--port", "8766", "--no-open", "--reviews", "/app/validation"]
