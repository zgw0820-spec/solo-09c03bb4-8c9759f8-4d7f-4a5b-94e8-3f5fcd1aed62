FROM python:3.12-slim

# GCC toolchain (libc6-dev provides the system headers).
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as an unprivileged user; /cache holds the persistent object cache.
RUN useradd --create-home appuser \
    && mkdir -p /cache /tmp/cbuild-work \
    && chown -R appuser:appuser /cache /tmp/cbuild-work
USER appuser

ENV CACHE_DIR=/cache/objects \
    WORK_DIR=/tmp/cbuild-work \
    PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
