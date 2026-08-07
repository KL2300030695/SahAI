# syntax=docker/dockerfile:1
#
# One image: the React dashboard is built and served by the FastAPI process.
#
# A hackathon judge should be able to run `docker compose up` and get the whole
# thing, which two containers and a reverse proxy would not deliver. It also
# removes CORS from the deployment entirely — same origin, no configuration to
# get wrong between a laptop and a server.

# --- stage 1: build the dashboard -------------------------------------------
FROM node:20-alpine AS web
WORKDIR /web
# package files first, so a source change does not re-run npm ci
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


# --- stage 2: the service ----------------------------------------------------
FROM python:3.11-slim AS app

# PYTHONUNBUFFERED so container logs appear in order rather than in bursts,
# which matters when the only view of a deployed run is `docker logs`.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Requirements first: the dependency layer is the expensive one and it changes
# far less often than the code.
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
COPY --from=web /web/dist ./static

# Build the knowledge base at image build time, not on first request. Otherwise
# the first call of a demo pays for embedding 19 documents, and a cold container
# looks broken rather than slow.
RUN python -m app.rag.ingest

# Run unprivileged. The data directory is created and owned before dropping, or
# SQLite cannot write and the failure surfaces as an unhelpful disk error.
RUN useradd --create-home --uid 10001 sahai \
    && mkdir -p /srv/data /srv/uploads \
    && chown -R sahai:sahai /srv
USER sahai

EXPOSE 8000

# Uses the readiness endpoint, not the liveness one: a container that is up but
# cannot reach its knowledge base should not receive traffic.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
