# syntax=docker/dockerfile:1.7
#
# Build context is this directory (mcp_servers/fed_calendar_connector/).
# From ../docker-compose.yml: `build: { context: ./fed_calendar_connector }`.

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install Python deps first so code edits don't invalidate this layer.
# Stub the package tree so the hatchling editable install has a path to
# link against; the real source is copied in below.
COPY pyproject.toml /app/
RUN mkdir -p src/fed_calendar_connector \
    && touch src/fed_calendar_connector/__init__.py \
    && pip install --no-cache-dir -e .

COPY src /app/src

EXPOSE 8767

ENTRYPOINT ["fed-calendar-connector"]
CMD ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8767"]
