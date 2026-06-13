FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY intercom_server.py intercom.html rooms.json ./

EXPOSE 8765

# Flask dev server is fine for LAN use; switch to gunicorn if needed
CMD ["python3", "intercom_server.py"]
