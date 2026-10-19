# discovery worker. Same image contents as every other worker: one package, one
# entry point per deployment unit.
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# Parsing needs the optional parser extras and a malware scanner; the others do
# not, so they stay smaller.

COPY packages/python /app/packages/python
COPY services/api /app/services/api
COPY infra/environments /app/infra/environments
COPY workers/discovery /app/workers/discovery
RUN pip install -e "/app/packages/python[anthropic]" -e /app/services/api

# Non-root: a parser runs untrusted documents.
RUN useradd --create-home --uid 10001 worker
USER worker

ENV LAB_ENV=server
ENTRYPOINT ["python", "/app/workers/discovery/main.py"]
