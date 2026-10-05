# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Default login. Change these two lines to set your own defaults, or override
# at run time with -e APP_USERNAME=... -e APP_PASSWORD=...
ENV APP_USERNAME=admin \
    APP_PASSWORD=admin123 \
    COOKIE_SECURE=false

WORKDIR /app

# Dependencies first so this layer is cached across code changes
COPY requirements.txt .
RUN pip install -r requirements.txt

# Application code
COPY timetable_scheduler/ ./timetable_scheduler/
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY main.py ./

# Run as a non-root user; backend/data holds the saved register/settings
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/backend/data \
    && chown -R appuser:appuser /app/backend/data
USER appuser

VOLUME ["/app/backend/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

# Single worker on purpose: login sessions and the in-memory timetable live in
# process memory, so multiple workers would log users out / lose the board.
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
