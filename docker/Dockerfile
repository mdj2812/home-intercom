FROM python:3.13-slim

WORKDIR /app

COPY src/requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

COPY src/intercom_server.py src/ha_client.py src/intercom.html src/rooms.json ./
COPY src/manifest.json src/static/ ./static/
COPY docker/.docker-image /app/.docker-image

EXPOSE 8764

ENV PYTHONUNBUFFERED=1

# Production WSGI via waitress
CMD ["python3", "intercom_server.py"]
