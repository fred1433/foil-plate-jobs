# Base image pinned by digest (python:3.12.12-slim-bookworm, multi-arch index).
FROM python@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

# Rendering stack: Ghostscript, Poppler, Cairo, LittleCMS. Debian bookworm versions;
# the exact installed versions are written to /app/RUNTIME_VERSIONS.txt at build time.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ghostscript poppler-utils libcairo2 liblcms2-2 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

COPY foilplate ./foilplate
COPY service ./service
COPY fixtures ./fixtures
COPY goldens ./goldens
COPY tests ./tests

ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT} PYTHONUNBUFFERED=1
RUN { echo "commit ${GIT_COMMIT}"; \
      dpkg-query -W -f='${Package} ${Version}\n' ghostscript libgs10 poppler-utils libpoppler126 libcairo2 liblcms2-2 2>/dev/null; \
      pip freeze; } > /app/RUNTIME_VERSIONS.txt

RUN useradd --uid 10001 --no-create-home app
USER app
EXPOSE 8080
# One request per instance (Cloud Run concurrency=1); a single process keeps a
# crash a real crash of the whole server.
CMD ["sh", "-c", "exec uvicorn service.app:app --host 0.0.0.0 --port ${PORT:-8080} --timeout-keep-alive 5"]
