# acquisition worker. Same image contents as every other worker: one package, one
# entry point per deployment unit.
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# This image fetches documents, and a fetched document is scanned before it is
# stored: the ClamAV client talks to the scanner container over its socket
# (/run/clamav, mounted in docker-compose.prod.yml). Without it every fetched
# file reported "clamdscan is not installed" and was set aside unscanned.
RUN apt-get update \
 && apt-get install -y --no-install-recommends clamdscan \
 && rm -rf /var/lib/apt/lists/*
COPY infra/compose/clamd-client.conf /etc/clamav/clamd.conf

COPY packages/python /app/packages/python
COPY services/api /app/services/api
COPY workers/acquisition /app/workers/acquisition
# `objectstore` (boto3) and `gcs` (google-cloud-storage) are the S3 and Google
# Cloud Storage SDKs. Every image carries both, so moving between the filesystem,
# S3 and GCS is a change to LAB_OBJECT_STORE_KIND, not a rebuild.
RUN pip install -e "/app/packages/python[parsers,objectstore,gcs]" -e /app/services/api

# Non-root: a parser runs untrusted documents.
RUN useradd --create-home --uid 10001 worker
USER worker

ENV LAB_ENV=server
ENTRYPOINT ["python", "/app/workers/acquisition/main.py"]
