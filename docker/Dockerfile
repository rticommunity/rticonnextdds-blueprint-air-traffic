# ── ATC Demo with RTI Connext DDS ────────────────────────────────────────────
#
# Build (from repo root):
#   docker build -f docker/Dockerfile -t atc-demo .
#
# Run the full demo (stages and mounts RTI_LICENSE_FILE from .env.local):
#   ./docker/run.sh
#
# Run a single component:
#   ./docker/run.sh dashboard
#
# Components: all (default), dashboard, center, tower, tracon, airport,
#             airplane, flightplan, weather

FROM python:3.14-slim

# Prevent Python from writing .pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (layer caching)
COPY python/requirements.txt python/
RUN pip install --no-cache-dir -r python/requirements.txt

# Copy only files required at runtime
COPY python/*.py python/
COPY scripts/demo_cli.py scripts/demo_start.sh scripts/
COPY docker/docker-entrypoint.sh .
COPY air_traffic_scenario.json air_traffic_qos.xml LICENSE ./

# Dashboard port
EXPOSE 8050

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["all"]
