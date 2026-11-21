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
# `objectstore` (boto3) and `gcs` (google-cloud-storage) are the S3 and Google
# Cloud Storage SDKs. Every image carries both, so moving between the filesystem,
# S3 and GCS is a change to LAB_OBJECT_STORE_KIND, not a rebuild.
RUN pip install -e "/app/packages/python[anthropic,objectstore,gcs]" -e /app/services/api

# Non-root: a parser runs untrusted documents.
RUN useradd --create-home --uid 10001 worker \
 && mkdir -p /var/lib/lab-assistant/objects /var/lib/lab-assistant/model-cache \
 && chown -R worker /var/lib/lab-assistant
USER worker

ENV LAB_ENV=server
ENTRYPOINT ["python", "/app/workers/discovery/main.py"]
