FROM python:3.13-slim

WORKDIR /app

COPY src/requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

# Docker-specific files
COPY src/intercom_server.py src/ha_client.py src/rooms.json ./
COPY src/site.webmanifest ./static/manifest.json
COPY docker/.docker-image /app/.docker-image

# Shared files — canonical source in custom_components/
COPY custom_components/home_intercom/intercom.html ./
COPY custom_components/home_intercom/static/ ./static/

EXPOSE 8764

ENV PYTHONUNBUFFERED=1

# Production WSGI via waitress
CMD ["python3", "intercom_server.py"]
