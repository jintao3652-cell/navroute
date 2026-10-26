FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    NRS_DATA_DIR=/data \
    NRS_MAX_LOADED_CYCLES=3

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini gosu \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker/entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
 && useradd --create-home --uid 1000 nrs \
 && mkdir -p /data/cycles /data/tmp \
 && chown -R nrs:nrs /data /app

# The container starts as root only long enough to make a bind-mounted /data
# writable (a host volume hides the ownership above), then drops to uid 1000
# via gosu before uvicorn is exec'd.  See docker/entrypoint.sh.

VOLUME ["/data"]
EXPOSE 8110

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8110/api/efb/health',timeout=4)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
# A single worker keeps one copy of the index in RAM; the service is
# I/O-light and handles >200 req/s per core with this configuration.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8110", \
     "--workers", "1", "--timeout-keep-alive", "75", \
     "--no-server-header"]
