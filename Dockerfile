FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PAPRIKA_TRANSPORT=http \
    PAPRIKA_HOST=0.0.0.0 \
    PAPRIKA_PORT=8080 \
    PAPRIKA_CACHE_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY *.py moods.json ./

# 568 = TrueNAS SCALE's built-in "apps" user, so the dataset permissions line up.
RUN mkdir -p /data && chown 568:568 /data
USER 568:568
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PAPRIKA_PORT\"]}/healthz', timeout=4)"

CMD ["python", "server.py"]
