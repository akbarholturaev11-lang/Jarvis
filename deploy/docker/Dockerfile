# JARVIS product backend — slim, non-root container image.
# Build context is the repository root:
#   docker build -f deploy/docker/Dockerfile -t jarvis-backend .
#
# The image contains only the backend + shared core + ops tooling; the desktop
# GUI/audio/vision code and its heavy dependencies are excluded. Secrets and the
# data directory are provided at runtime as mounts / environment, never baked in.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/jarvis

# Install backend dependencies first for layer caching.
COPY deploy/docker/requirements-backend.txt ./requirements-backend.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements-backend.txt

# Copy only the code the backend needs.
COPY product_backend/ ./product_backend/
COPY core/ ./core/
COPY ops/ ./ops/

# Run as a dedicated non-root user; the data volume is owned at runtime.
RUN useradd --system --create-home --uid 10001 jarvis \
    && mkdir -p /var/lib/jarvis/data /var/lib/jarvis/artifacts \
    && chown -R jarvis:jarvis /var/lib/jarvis \
    && chmod 700 /var/lib/jarvis/data /var/lib/jarvis/artifacts
USER jarvis

VOLUME ["/var/lib/jarvis"]
EXPOSE 8080

# The container listens on plain HTTP; a TLS reverse proxy terminates HTTPS and
# forwards here. The liveness probe hits the host-header-agnostic /healthz.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"

CMD ["python", "-m", "uvicorn", \
     "product_backend.runtime:create_app_from_environment", "--factory", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
