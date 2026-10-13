FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Mount a persistent volume at /data so memory survives redeploys.
ENV DB_PATH=/data/menumind.db \
    COOKIE_SECURE=1 \
    PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
