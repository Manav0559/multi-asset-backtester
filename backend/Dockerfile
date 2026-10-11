# Backend image: the single FastAPI web process (backtests run in-process via
# BackgroundTasks; periodic jobs on the asyncio scheduler). Python 3.13 to
# match dev.
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

# pandas-ta's @njit(cache=True) kernels write their numba cache next to the
# package source by default; site-packages is root-owned, so as the app user
# `import pandas_ta` fails ("no locator available") without a writable dir.
ENV NUMBA_CACHE_DIR=/tmp/numba_cache

# Unprivileged runtime user. Fixed uid/gid so volume ownership is predictable;
# 10001 stays clear of the distro's system and regular user ranges.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /home/app --create-home \
       --shell /usr/sbin/nologin app \
    && mkdir -p "$NUMBA_CACHE_DIR" \
    && chown app:app "$NUMBA_CACHE_DIR"

# Source stays root-owned and read-only to the app user: nothing in the app
# writes under /app at runtime, so the process cannot modify its own code.
COPY . .

USER app:app

EXPOSE 8000

# Entrypoint migrates to head, then serves (see start.sh). Exec-form CMD so the
# host runs it verbatim — no shell re-parsing of a quoted command string, which
# is what made the old inline `sh -c '...'` exit 127.
CMD ["sh", "start.sh"]
