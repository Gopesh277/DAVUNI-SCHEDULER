FROM python:3.11-slim

# Prevent Python from creating .pyc files and buffer logs
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first for better Docker caching
COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# FastAPI port
EXPOSE 8000

# Start FastAPI
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
