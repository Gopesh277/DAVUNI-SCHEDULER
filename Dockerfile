# syntax=docker/dockerfile:1
#
# DAV University Timetable Generator
# FastAPI backend + static frontend, solved with Google OR-Tools CP-SAT.
#
#   docker build -t davuni-scheduler .
#   docker run -d -p 8000:8000 -v scheduler-data:/app/backend/data \
#       -e APP_PASSWORD='a-real-password' \
#       davuni-scheduler
#
# then open http://localhost:8000

############################
# Stage 1: build the venv  #
############################
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependencies first, so this layer is cached until requirements.txt changes
COPY requirements.txt .
RUN pip install -r requirements.txt


############################
# Stage 2: runtime image   #
############################
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Login settings.
#  * Passwords are deliberately NOT set here, so none is baked into the image.
#    Pass them at run time (-e / compose / .env). If left unset the app falls
#    back to admin / admin123 -- fine for a local
#    test, never for a reachable server.
#  * Once an admin uses "Change password" in the app, the new (hashed) password
#    is saved in /app/backend/data/credentials.json and wins over APP_PASSWORD.
#  * COOKIE_SECURE=false lets login work over plain http. Set it to "true" when
#    HTTPS (a reverse proxy) sits in front.
ENV APP_USERNAME=admin \
    COOKIE_SECURE=false

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# Application code (backend/ includes departments.json, the department list)
COPY timetable_scheduler/ ./timetable_scheduler/
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY main.py ./

# Run as a non-root user. backend/data holds everything that must survive a
# restart: each department's rooms/register/settings (dept_<id>.json) and
# changed admin passwords (credentials.json). Mount a volume here.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/backend/data \
    && chown -R appuser:appuser /app/backend/data
USER appuser

VOLUME ["/app/backend/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

# One worker on purpose: login sessions and each department's in-memory
# timetable live in process memory, so several workers would randomly sign
# users out and lose the board.
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
