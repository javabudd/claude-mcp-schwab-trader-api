# syntax=docker/dockerfile:1.7
#
# Build context is this directory (mcp_servers/yahoo_connector/).
# From ../docker-compose.yml: `build: { context: ./yahoo_connector }`.

FROM condaforge/miniforge3:24.9.0-0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONDA_ENV=traider

# Same base env the schwab-connector image uses: Python 3.13 plus the
# TA-Lib C library and Python wrapper from conda-forge. yfinance and
# the rest come from pip below.
RUN conda create -n "${CONDA_ENV}" -c conda-forge -y \
        python=3.13 \
        ta-lib \
    && conda clean -afy

SHELL ["conda", "run", "--no-capture-output", "-n", "traider", "/bin/bash", "-c"]

WORKDIR /app

# Install Python deps first so code edits don't invalidate this layer.
# Stub the package tree so the hatchling editable install has a path to
# link against; the real source is copied in below.
COPY pyproject.toml /app/
RUN mkdir -p src/yahoo_connector \
    && touch src/yahoo_connector/__init__.py \
    && pip install --no-cache-dir -e .

COPY src /app/src

EXPOSE 8765

ENTRYPOINT ["conda", "run", "--no-capture-output", "-n", "traider", "yahoo-connector"]
CMD ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8765"]
