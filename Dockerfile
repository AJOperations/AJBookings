FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent volume for SQLite DB
RUN mkdir -p /app/data

EXPOSE 5000

CMD ["gunicorn", "app:app", "--workers", "1", "--preload", "--timeout", "300", "--bind", "0.0.0.0:5000"]
