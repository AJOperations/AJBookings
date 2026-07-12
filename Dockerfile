FROM python:3.11-slim

WORKDIR /app

# git — pip needs this to resolve requirements.txt's
# git+https://github.com/AJOperations/aj-shared@v1.1.0 pin;
# python:3.11-slim doesn't include it by default (aj-shared retrofit, 2026-07-11).
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent volume for SQLite DB
RUN mkdir -p /app/data

EXPOSE 5000

CMD ["gunicorn", "app:app", "--workers", "1", "--preload", "--timeout", "300", "--bind", "0.0.0.0:5000"]
