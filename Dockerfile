FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py catalog.json ./

# Cloud Run sets PORT (8080 by default).
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
