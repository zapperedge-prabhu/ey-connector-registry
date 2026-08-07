# testfile ServiceNow CSV Pre-processing Connector
FROM python:3.11-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

RUN useradd --create-home --shell /bin/bash connector \
    && mkdir -p /data \
    && chown -R connector:connector /app /data

USER connector
VOLUME /data

ENV SOURCE_DIR=/data/servicenow

ENTRYPOINT ["python", "connector.py"]
