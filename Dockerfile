FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# World-writable /data and /cache so the container works under any
# uid:gid override (e.g. user: "99:100" on Unraid), not just the default.
RUN useradd -r -u 1000 -m altrepo \
    && mkdir -p /data /cache \
    && chown altrepo /data /cache \
    && chmod 0777 /data /cache
USER altrepo

ENV DATA_DIR=/data CACHE_DIR=/cache
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
