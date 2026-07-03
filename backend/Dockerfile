# Backend image: FastAPI web process AND the Celery worker (same code, different
# CMD chosen in docker-compose). Python 3.13 to match dev.
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Runtime libs only: psycopg2-binary + the scientific stack (pandas/numpy/scipy/
# scikit-learn/xgboost) ship manylinux wheels, so no compiler is needed — but
# xgboost needs libgomp (OpenMP) at runtime, and curl backs the healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# mmap target for prometheus multiprocess mode (used by the Celery worker).
RUN mkdir -p /tmp/prometheus

EXPOSE 8000

# Default is the web process; the worker service overrides this in compose.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
