# syntax=docker/dockerfile:1.7
#
# Single unified image for the traider hub. All provider deps are
# installed once; which tool groups actually run inside is gated by
# TRAIDER_PROVIDERS at startup (see src/traider/server.py).

FROM condaforge/miniforge3:24.9.0-0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONDA_ENV=traider

# Python + TA-Lib C library + wrapper from conda-forge. TA-Lib needs
# the native C lib; conda-forge is the project-recommended install
# path. yfinance, lxml, beautifulsoup4, mcp, httpx, etc. are pip-only
# and installed from pyproject.toml below.
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
RUN mkdir -p src/traider \
    && touch src/traider/__init__.py \
    && pip install --no-cache-dir -e .

COPY src /app/src

# Framework rules — the canonical seed ships with the image. Per-user
# overrides live at /root/.traider/rules.local (gitignored on the host,
# mounted into the container by docker-compose so they persist across
# container restarts without rebuilding the image).
COPY rules /app/rules

# Entrypoint mints a self-signed TLS cert on first start (into the
# /certs volume) and execs traider with --ssl-certfile/--ssl-keyfile
# attached. The container always serves HTTPS so Claude Desktop's
# remote-MCP integration can connect.
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8765

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8765"]
