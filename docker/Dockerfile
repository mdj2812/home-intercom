FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/intercom_server.py src/intercom.html src/rooms.json ./
COPY src/manifest.json src/static/ ./static/

EXPOSE 8764

# Production WSGI via waitress
CMD ["python3", "intercom_server.py"]
