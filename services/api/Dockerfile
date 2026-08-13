FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# An assisted upload is scanned on the request path, before any parse task is
# queued, so the scanner has to be in *this* image and not only in the parsing
# worker. Without it the scanner reports itself unavailable and the upload is
# visibly blocked — correct, but useless to the person holding the PDF.
RUN apt-get update \
 && apt-get install -y --no-install-recommends clamav clamav-daemon \
 && rm -rf /var/lib/apt/lists/*
COPY packages/python /app/packages/python
COPY services/api /app/services/api
RUN pip install -e /app/packages/python -e /app/services/api

RUN useradd --create-home --uid 10001 api \
 && mkdir -p /var/lib/lab-assistant/objects /var/lib/lab-assistant/model-cache \
 && chown -R api /var/lib/lab-assistant
USER api

ENV LAB_ENV=server
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/health')"

# Factory form: create_app() validates production settings before serving.
CMD ["uvicorn", "lab_assistant_api.main:app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
