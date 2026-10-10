# The platform is read-only over an existing corpus, so the image carries the
# code and its dependencies and nothing else: the database and the raw files
# stay on the host and are mounted in.  An image with a 150 MB SQLite file baked
# into it would be a copy of the evidence that nobody can update.
FROM python:3.12-slim AS web
WORKDIR /build
# Node is only needed to build the client, and only the client's own inputs are
# copied, so editing Python code does not invalidate the npm layer.
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*
COPY web/package.json web/package-lock.json* ./web/
RUN cd web && npm install
COPY web ./web
RUN cd web && npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY api ./api
COPY collector ./collector
COPY processor ./processor
COPY config ./config
COPY --from=web /build/web/dist ./web/dist
# Not root: the container only ever reads the corpus, and the one thing it
# writes — the review and notification tables — lives in a mounted volume.
RUN useradd --create-home --uid 10001 platform && chown -R platform /app
USER platform
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"
CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
