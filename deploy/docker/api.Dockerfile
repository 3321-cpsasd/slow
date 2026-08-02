# syntax=docker/dockerfile:1

FROM python:3.12-slim

ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 slow \
    && useradd --uid 10001 --gid slow --create-home --shell /usr/sbin/nologin slow

WORKDIR /srv/slow

COPY apps/api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r /tmp/requirements.txt

COPY apps/api ./apps/api
COPY deploy/docker/api-entrypoint.sh /usr/local/bin/slow-api-entrypoint

RUN chmod 0555 /usr/local/bin/slow-api-entrypoint \
    && mkdir -p /data/attachments /data/backups \
    && chown -R slow:slow /data

USER slow
EXPOSE 8000

ENTRYPOINT ["slow-api-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
